"""Loopback HTTP proxy and same-origin dashboard API."""

import asyncio
import gzip
import json
import logging
import os
import sqlite3
import time
import zlib
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
import zstandard
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from backend.db import Database
from backend.utils import (
    DEFAULTS,
    ROOT,
    ROUTES,
    UPSTREAM,
    RouterError,
    compress_body,
    tokens,
)

LOG = logging.getLogger("ctx-engine")
DB_PATH = os.getenv("PI_CONTEXT_ROUTER_DB", "~/.pi/agent/ctx-engine-v2.db")
MAX_BODY = int(os.getenv("PI_CONTEXT_ROUTER_MAX_BODY_BYTES", "134217728"))
HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tokens: int = Field(default=DEFAULTS["tokens"], ge=1024, le=4000000)
    keepTurns: int = Field(default=DEFAULTS["keepTurns"], ge=1, le=100)
    maxTokens: int = Field(default=DEFAULTS["maxTokens"], ge=128, le=65536)
    model: str = DEFAULTS["model"]
    blockPrompt: str = Field(
        default=DEFAULTS["blockPrompt"], min_length=1, max_length=16000
    )

    def validated(self):
        if (
            self.model not in ("deepseek-flash", "gpt-6-luna")
            or not self.blockPrompt.strip()
        ):
            raise RouterError(400, "Invalid condenser settings")
        return self.model_dump()


@asynccontextmanager
async def lifespan(app):
    app.state.db = Database(DB_PATH)
    Settings(**app.state.db.settings(DEFAULTS)).validated()
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(300, connect=10),
        limits=httpx.Limits(max_connections=None, max_keepalive_connections=20),
        follow_redirects=False,
        trust_env=False,
    )
    LOG.info("Python context engine ready; database=%s", DB_PATH)
    yield
    await app.state.client.aclose()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def guard_management(request, call_next):
    if request.url.path.startswith(("/api/", "/dashboard")) or request.url.path in (
        "/stats",
        "/health",
    ):
        origin = request.headers.get("origin")
        if request.url.hostname not in ("127.0.0.1", "localhost", "::1"):
            return JSONResponse({"error": "Loopback access only"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin
            and (
                urlsplit(origin).netloc != request.headers.get("host")
                or urlsplit(origin).scheme != request.url.scheme
            )
        ):
            return JSONResponse({"error": "Same-origin access only"}, status_code=403)
    return await call_next(request)


@app.exception_handler(RouterError)
async def router_error(request, error):
    return JSONResponse({"error": error.message}, status_code=error.status)


@app.exception_handler(sqlite3.Error)
async def database_error(request, error):
    LOG.error("SQLite operation failed: %s", type(error).__name__)
    return JSONResponse({"error": "Database unavailable"}, status_code=503)


@app.get("/health")
async def health(request: Request, deep: bool = False):
    check = request.app.state.db.quick_check if deep else request.app.state.db.health
    integrity = await asyncio.to_thread(check)
    return JSONResponse(
        {
            "status": "ok" if integrity == "ok" else "error",
            "database": "ok" if integrity == "ok" else "integrity check failed",
        },
        status_code=200 if integrity == "ok" else 503,
    )


@app.get("/stats")
async def stats(request: Request, range: str = "24h"):
    if range not in ("24h", "7d", "30d"):
        raise RouterError(400, "Invalid stats range")
    return request.app.state.db.stats(range)


@app.get("/api/settings")
async def get_settings(request: Request):
    return {"settings": request.app.state.db.settings(DEFAULTS), "defaults": DEFAULTS}


@app.put("/api/settings")
async def put_settings(settings: Settings, request: Request):
    db = request.app.state.db
    merged = {**db.settings(DEFAULTS), **settings.model_dump(exclude_unset=True)}
    db.save_settings(Settings(**merged).validated())
    return await get_settings(request)


@app.get("/api/history")
async def history(
    request: Request,
    limit: int = 50,
    before: int | None = None,
    changed: str = "all",
    search: str = "",
):
    if (
        not 1 <= limit <= 100
        or (before is not None and before < 1)
        or changed not in ("all", "yes", "no")
        or len(search) > 200
    ):
        raise RouterError(400, "Invalid history query")
    return request.app.state.db.history(limit, before, changed, search)


@app.get("/api/history/{request_id}")
async def detail(request_id: int, request: Request):
    row = request.app.state.db.detail(request_id)
    if row is None:
        raise RouterError(404, "Request not found")
    return row


def headers_without_hop(headers, response=False):
    extra = {v.strip().lower() for v in headers.get("connection", "").split(",")}
    excluded = HOP | extra
    if response:
        excluded = excluded - {"content-length"}
    return [(k, v) for k, v in headers.raw if k.decode().lower() not in excluded]


async def read_body(request):
    chunks, length = [], 0
    async for chunk in request.stream():
        length += len(chunk)
        if length > MAX_BODY:
            raise RouterError(400, "Request exceeds transport byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def decode_body(raw, encoding):
    if not raw:
        return raw
    if encoding == "identity":
        return raw
    try:
        if encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            decoded = decoder.decompress(raw, MAX_BODY + 1)
            if len(decoded) > MAX_BODY or decoder.unconsumed_tail:
                raise RouterError(400, "Decoded request exceeds transport byte limit")
            if not decoder.eof or decoder.unused_data:
                raise RouterError(400, "Invalid gzip request")
            return decoded
        if encoding == "zstd":
            import io

            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(raw)) as reader:
                decoded = reader.read(MAX_BODY + 1)
            if len(decoded) > MAX_BODY:
                raise RouterError(400, "Decoded request exceeds transport byte limit")
            return decoded
    except (zlib.error, zstandard.ZstdError):
        raise RouterError(400, "Invalid compressed request") from None
    raise RouterError(415, "Unsupported content encoding")


async def proxy(request, upstream, rest):
    db, client = request.app.state.db, request.app.state.client
    started = time.monotonic()
    request_id = db.start_request(f"{request.method} {request.url.path}")
    trace = {
        "model": "",
        "status": 0,
        "changed": False,
        "before_tokens": 0,
        "after_tokens": 0,
        "duration_ms": 0,
        "cache_hits": 0,
        "error": None,
        "original": None,
        "compressed": None,
        "changes": [],
    }
    upstream_response = None

    def persist():
        trace["duration_ms"] = round((time.monotonic() - started) * 1000)
        db.finish_request(request_id, trace)

    try:
        body = await read_body(request)
        encoding = request.headers.get("content-encoding", "identity").lower()
        decoded = decode_body(body, encoding)
        # Empty-body methods (including auth/logout) forward without JSON parsing.
        value = None
        if decoded:
            try:
                value = json.loads(decoded)
            except (ValueError, UnicodeDecodeError):
                pass
            trace["original"] = (
                value if value is not None else decoded.decode(errors="replace")
            )
            trace["compressed"] = trace["original"]
            trace["before_tokens"] = tokens(trace["original"])
            trace["after_tokens"] = trace["before_tokens"]
        if isinstance(value, dict):
            trace["model"] = str(value.get("model", ""))
            # Preserve the full received body before starting possibly slow inference.
            persist()
            compatible = dict(value)
            credential = request.headers.get("authorization", "").split(" ", 1)
            if (
                request.url.path in ("/r/openai/responses", "/codex/responses")
                and len(credential) == 2
                and credential[0].lower() == "bearer"
                and not credential[1].startswith("sk-")
            ):
                removed = {
                    k: compatible.pop(k)
                    for k in (
                        "max_output_tokens",
                        "temperature",
                        "prompt_cache_retention",
                        "prompt_cache_options",
                    )
                    if k in compatible
                }
                if removed:
                    trace["changes"].append(
                        {
                            "kind": "subscription",
                            "before": removed,
                            "after": None,
                            "cached": False,
                        }
                    )
            config = Settings(**db.settings(DEFAULTS)).validated()
            forwarded, changes, hits = await compress_body(
                compatible, config, db, client
            )
            trace["changes"].extend(changes)
            trace["cache_hits"] = hits
            trace["changed"] = bool(trace["changes"])
            trace["compressed"] = forwarded
            trace["after_tokens"] = tokens(forwarded)
            if trace["changed"]:
                body = json.dumps(
                    forwarded, ensure_ascii=False, separators=(",", ":")
                ).encode()
                if encoding == "gzip":
                    body = gzip.compress(body)
                elif encoding == "zstd":
                    body = zstandard.ZstdCompressor().compress(body)
        target = upstream.rstrip("/") + "/" + rest.lstrip("/")
        if request.url.query:
            target += "?" + request.url.query
        outbound = client.build_request(
            request.method,
            target,
            headers=headers_without_hop(request.headers),
            content=body,
        )
        upstream_response = await client.send(outbound, stream=True)
        trace["status"] = upstream_response.status_code
        if trace["status"] >= 400:
            trace["error"] = f"Upstream returned HTTP {trace['status']}"
        persist()
        LOG.info(
            "request=%s method=%s route=%s status=%s tokens=%s->%s cache_hits=%s",
            request_id,
            request.method,
            request.url.path,
            trace["status"],
            trace["before_tokens"],
            trace["after_tokens"],
            hits if isinstance(value, dict) else 0,
        )
    except BaseException as error:
        if upstream_response is not None:
            await upstream_response.aclose()
        if isinstance(error, RouterError):
            trace["status"], trace["error"] = error.status, error.message
        elif isinstance(error, sqlite3.Error):
            trace["status"], trace["error"] = 503, "Database unavailable"
        elif isinstance(error, asyncio.CancelledError):
            trace["status"], trace["error"] = 499, "Request cancelled"
        else:
            trace["status"], trace["error"] = 502, "Upstream connection failed"
        persist()
        LOG.error(
            "request=%s route=%s status=%s failure=%s",
            request_id,
            request.url.path,
            trace["status"],
            type(error).__name__,
        )
        if isinstance(error, (RouterError, sqlite3.Error, asyncio.CancelledError)):
            raise
        raise RouterError(502, "Upstream connection failed") from None

    raw_headers = headers_without_hop(upstream_response.headers, response=True)
    raw_headers.append(
        (b"x-context-router", b"compacted" if trace["changed"] else b"pass")
    )
    if request.method == "HEAD" or upstream_response.status_code in (204, 304):
        await upstream_response.aclose()
        response = Response(status_code=upstream_response.status_code)
        response.raw_headers = raw_headers
        return response

    async def stream():
        try:
            async for chunk in upstream_response.aiter_raw():
                yield chunk
        except asyncio.CancelledError:
            trace["status"], trace["error"] = 499, "Client disconnected during stream"
            raise
        except httpx.HTTPError:
            trace["status"], trace["error"] = 502, "Upstream stream failed"
            LOG.error("request=%s upstream stream failed", request_id)
            raise
        finally:
            await upstream_response.aclose()
            persist()

    response = StreamingResponse(stream(), status_code=upstream_response.status_code)
    response.raw_headers = raw_headers
    return response


@app.api_route(
    "/r/{provider}/{rest:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def provider_proxy(provider: str, rest: str, request: Request):
    if provider not in ROUTES:
        raise RouterError(404, "Unknown provider")
    return await proxy(request, ROUTES[provider], rest)


@app.api_route("/codex/responses", methods=["GET", "POST"])
async def codex_proxy(request: Request):
    return await proxy(request, UPSTREAM, "codex/responses")


@app.get("/")
@app.get("/dashboard")
async def dashboard_redirect():
    return RedirectResponse("/dashboard/")


DIST = ROOT / "frontend/dist"
if (DIST / "assets").is_dir():
    app.mount(
        "/dashboard/assets", StaticFiles(directory=DIST / "assets"), name="assets"
    )


@app.get("/dashboard/{path:path}")
async def dashboard(path: str):
    if path.startswith("assets/"):
        raise RouterError(404, "Asset not found")
    if not (DIST / "index.html").is_file():
        raise RouterError(503, "Build frontend first: cd frontend && npm run build")
    return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    import uvicorn

    host = os.getenv("PI_CONTEXT_ROUTER_HOST", "127.0.0.1")
    if host not in ("127.0.0.1", "::1"):
        raise SystemExit("Context engine must bind loopback only")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    # httpx logs full URLs (including queries); keep only our safe metadata logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(
        app,
        host=host,
        port=int(os.getenv("PI_CONTEXT_ROUTER_PORT", "4141")),
        access_log=False,
    )
