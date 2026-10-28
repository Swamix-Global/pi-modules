"""pi ctx engine — minifies agent context before the inference host sees it.

One stateless rule, applied blindly to every managed chat request: keep the newest
complete tool pair and drop every older one (N-1 stripping). Prose is never cut,
media blocks before the current turn are stripped, incomplete pairs are never cut,
and there are no token budgets — requests with nothing to strip are forwarded
byte-for-byte, so consecutive turns share an identical prompt prefix. An optional
`_pi_ctx` snapshot supplies reported usage for telemetry only: actual provider
usage, not byte equality or size estimates, establishes cache reuse.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import utils
from .config import (CODEX_HOST, HOST,
                     MAX_BODY_BYTES, PORT, ROUTES, UPSTREAM_TIMEOUT)


# --- shapes -------------------------------------------------------------------------------
class RequestInfo(BaseModel):
    """What one request shed: tool pairs beyond the newest, and pre-turn media."""

    trigger: str = "passthrough"  # passthrough | stripped
    pairs_dropped: int = 0
    media_removed: int = 0
    messages_before: int = 0
    messages_after: int = 0
    model: str = ""


class Stats(BaseModel):
    """`/stats` counters, mutated once per request."""

    requests: int = 0
    unchanged: int = 0
    stripped: int = 0
    pairs_dropped: int = 0
    media_removed: int = 0
    last_request: RequestInfo | None = None
    usage_tokens: int = 0
    status: dict[str, int] = Field(default_factory=dict)
    active: bool = False
    model: str = ""


class Health(BaseModel):
    """`/health` view: local host liveness plus lifetime counters."""

    ok: bool
    usage_tokens: int
    requests: int
    active: bool
    model: str


STATS = Stats()
SESSION = requests.Session()


# --- minification pipeline ----------------------------------------------------------------
def minify(raw: bytes, headers: dict[str, str], provider: str) -> tuple[bytes, RequestInfo, int, bool]:
    """Apply N-1 tool stripping: keep only the newest complete tool pair."""
    text = raw.decode("utf-8", errors="strict")
    snapshot = None
    try:
        snapshot = utils.parse_snapshot(text)
        body = snapshot["body"]
    except ValueError:
        body = json.loads(text)
    if not isinstance(body, dict):
        raise ValueError("request must be an object")
    original = body.get("messages")
    if not isinstance(original, list) or not original:
        raise ValueError("no chat messages")
    messages, media = utils.strip_media(original)
    messages, pairs = utils.strip_to_newest_pair(messages)
    messages = utils.repair_tool_pairs(messages)  # identity-safe; guards client-side orphans
    info = RequestInfo(trigger="passthrough" if messages is original else "stripped",
                       pairs_dropped=pairs, media_removed=media,
                       messages_before=len(original), messages_after=len(messages),
                       model=str(body.get("model", "")))
    usage = snapshot["usage"] if snapshot else 0  # zero means unavailable, NOT an estimate
    if messages is original and snapshot is None:
        return raw, info, usage, bool(body.get("stream"))
    body["messages"] = messages
    return json.dumps(body, ensure_ascii=False).encode("utf-8"), info, usage, bool(body.get("stream"))


def _record(info: RequestInfo, usage: int) -> None:
    """Only Pi's provider usage describes billed/cache tokens; everything else is counts."""
    STATS.usage_tokens += usage
    STATS.unchanged += int(info.trigger == "passthrough")
    STATS.stripped += int(info.trigger == "stripped")
    STATS.pairs_dropped += info.pairs_dropped
    STATS.media_removed += info.media_removed
    STATS.last_request = info


# --- inference host plumbing --------------------------------------------------------------
def _count(status: int) -> None:
    STATS.status[str(status)] = STATS.status.get(str(status), 0) + 1


def _tag(info: RequestInfo, status: int) -> dict[str, str]:
    _count(status)
    return {"x-pi-router": info.trigger}


def _chain(upstream: requests.Response, first: bytes, rest):
    """Starlette iterates this sync generator in its threadpool, not the event loop."""
    try:
        yield first
        yield from rest
    finally:
        upstream.close()


def _respond(upstream: requests.Response, first: bytes, rest,
             info: RequestInfo) -> Response:
    """Mirror host status and bytes, tagging the trigger and counting the status."""
    status = upstream.status_code
    media_type = upstream.headers.get("content-type", "application/json")
    headers = _tag(info, status)
    return StreamingResponse(_chain(upstream, first, rest), status_code=status, headers=headers,
                             media_type=media_type)


async def proxy(provider: str, rest: str, request: Request, minify_body: bool) -> Response:
    """Forward one request, stripping managed chat messages before upstream dispatch."""
    base = ROUTES.get(provider)
    if not base:
        return JSONResponse({"error": {"message": f"unknown provider {provider}"}}, status_code=404)
    raw = await request.body()
    STATS.requests += 1
    body, info, streams = raw, RequestInfo(), False
    if minify_body and len(raw) <= MAX_BODY_BYTES:
        try:
            body, info, usage, streams = await run_in_threadpool(minify, raw, dict(request.headers), provider)
        except (ValueError, UnicodeError, TypeError, AttributeError):
            if request.headers.get("x-pi-ctx-session"):
                _count(400)
                return JSONResponse({"error": {"message": "Invalid managed chat request"}}, status_code=400)
            pass  # other wire formats are forwarded without chat transformations
        else:
            _record(info, usage)
    clean_headers = {k: v for k, v in request.headers.items() if not k.lower().startswith("x-pi-ctx-")}
    headers = utils.upstream_headers(provider, clean_headers, utils.auth_map())
    try:
        upstream = await run_in_threadpool(SESSION.post, f"{base.rstrip('/')}/{rest}", headers=headers, stream=True,
                                           data=utils.maybe_gzip(provider, body, headers), timeout=UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        _count(502)
        return JSONResponse({"error": {"message": f"inference host {provider}: {exc}"}},
                            status_code=502)
    chunks = upstream.iter_content(65536)
    first = await run_in_threadpool(next, chunks, b"")
    if not minify_body or streams:
        return _respond(upstream, first, chunks, info)
    try:
        payload = first + await run_in_threadpool(b"".join, chunks)
    finally:
        upstream.close()
    return Response(payload, status_code=upstream.status_code,
                    headers=_tag(info, upstream.status_code),
                    media_type=upstream.headers.get("content-type", "application/json"))


def _ping() -> None:
    """Refresh served-model name and liveness of the local SGLang host."""
    model = utils.served_model()
    STATS.active = model != "default"
    STATS.model = model if STATS.active else STATS.model


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ping the local host once so /health is meaningful at boot."""
    await run_in_threadpool(_ping)
    yield


app = FastAPI(title="pi ctx engine", version="3.0", lifespan=lifespan)


@app.get("/health")
def health() -> Health:
    """Liveness of the local model host plus lifetime counters."""
    return Health(ok=True, usage_tokens=STATS.usage_tokens, requests=STATS.requests,
                  active=STATS.active, model=STATS.model)


@app.get("/stats")
def stats() -> Stats:
    """Every counter the pipeline mutates, per request."""
    return STATS


@app.get("/")
def root() -> dict:
    """Service identity: mode and reachable endpoints."""
    return {"service": "pi-ctx-engine", "mode": "N-1",
            "strategy": "stateless N-1 tool stripping: keep the newest complete tool pair, drop older pairs, never cut prose, no token budgets",
            "endpoints": ["/health", "/stats", "/r/{provider}/{rest}",
                          "/v1/chat/completions", "/codex/responses"]}


@app.post("/r/{provider}/{rest:path}")
async def route_provider(provider: str, rest: str, request: Request) -> Response:
    """Provider pass-through with N-1 stripping."""
    return await proxy(provider, rest, request, minify_body=True)


@app.post("/v1/chat/completions")
async def route_sglang(request: Request) -> Response:
    """Local SGLang pass-through, never stripped (it is the router's model host)."""
    return await proxy("sglang", "chat/completions", request, minify_body=True)


@app.post("/codex/responses")
async def route_codex(request: Request) -> Response:
    """Codex responses forwarded verbatim to the ChatGPT backend."""
    raw = await request.body()
    STATS.requests += 1
    auth = utils.auth_map().get("openai-codex")
    headers = utils.upstream_headers("openai-codex", dict(request.headers),
                               {"openai-codex": auth} if auth else {})
    try:
        upstream = await run_in_threadpool(SESSION.post, CODEX_HOST, data=raw, headers=headers, stream=True,
                                           timeout=UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        _count(502)
        return JSONResponse({"error": {"message": f"codex host: {exc}"}}, status_code=502)
    chunks = upstream.iter_content(65536)
    first = await run_in_threadpool(next, chunks, b"")
    return _respond(upstream, first, chunks, RequestInfo())


def serve() -> None:
    """Run the router as a standalone service."""
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)
