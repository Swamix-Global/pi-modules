"""pi ctx engine — minifies agent context before the inference host sees it.

A turn is a user message through its last successful assistant response. Fast Mode (L1)
cuts complete tool pairs before the current turn; Standard Mode (L2) then folds surviving
prose into notes through the local model. Private Pi headers carry session/model limits;
an optional `_pi_ctx` snapshot supplies reported usage for telemetry only. Unchanged
requests are forwarded byte-for-byte; actual provider usage, not byte equality or size
estimates, establishes cache reuse.
"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from . import utils
from .config import (BUDGET, CODEX_HOST, HOST,
                     LEVEL, MAX_BODY_BYTES, PORT, ROUTES, UPSTREAM_TIMEOUT)


# --- shapes -------------------------------------------------------------------------------
class CondenseInfo(BaseModel):
    """What one request lost: dropped blocks, folded entries, trigger, cost."""

    dropped_blocks: int = 0
    condensed_entries: int = 0
    summary_words: int = 0
    duration_ms: float = 0.0
    trigger: str = "passthrough"
    input_estimate: int = 0
    output_estimate: int = 0
    budget: int = 0
    newly_dropped: int = 0
    reapplied_messages: int = 0
    summary_reused: bool = False
    media_removed: int = 0
    shared_prefix_estimate: int = 0
    frontier_advanced: bool = False
    session: str = ""
    model: str = ""


class Stats(BaseModel):
    """`/stats` counters, mutated once per request."""

    requests: int = 0
    passthrough: int = 0
    strategy_passes: int = 0
    condenses: int = 0
    dropped_blocks: int = 0
    trims: int = 0            # actual frontier advances, not repeated reapplication
    frontier_reuses: int = 0
    guard_failures: int = 0
    persistence_errors: int = 0
    last_request: CondenseInfo | None = None
    usage_tokens: int = 0
    status: dict[str, int] = Field(default_factory=dict)
    last_condense: CondenseInfo | None = None
    active: bool = False
    model: str = ""


class Health(BaseModel):
    """`/health` view: condenser liveness plus lifetime counters."""

    ok: bool
    usage_tokens: int
    compactions: int
    active: bool
    model: str


STATS = Stats()
SESSION = requests.Session()


# --- minification pipeline ----------------------------------------------------------------
def minify(raw: bytes, headers: dict[str, str], provider: str) -> tuple[bytes, CondenseInfo, int, bool]:
    """Apply a session/model-isolated stable representation, then fit the model window."""
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
    window = int(headers.get("x-pi-ctx-window", "0"))
    reserve = int(headers.get("x-pi-ctx-reserve", "0"))
    if window and not (4096 <= window <= 4_000_000 and 0 <= reserve < window):
        raise ValueError("invalid context window metadata")
    schema = utils.token_estimate(json.dumps([body.get("tools", []), body.get("response_format", {})], ensure_ascii=False))
    # Account for schemas and framing, not just message text. Missing metadata uses
    # the operator budget; explicit Pi metadata can only lower it, never raise it.
    budget = min(BUDGET, window - reserve - schema - 1024) if window else BUDGET - schema
    if budget < 1:
        raise utils.ContextOverflow("Tool schemas and output reserve exhaust this model's context window.")
    root = next((m for m in original if m.get("role") == "user"), original[0])
    identity = [provider, body.get("model"), headers.get("x-pi-ctx-session", ""), window, reserve, utils.content_sig(root)]
    key = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    with utils.locked_state(key) as state:
        saved = state.dropped.copy(), state.folded, state.note
        try:
            messages, details, changed = state.fit(original, budget)
        except utils.ContextOverflow:
            state.dropped, state.folded, state.note = saved
            raise
        if changed and not utils.persist(key, state):
            STATS.persistence_errors += 1
    trigger = ("strategy+condenser" if details["condensed_entries"] else "strategy" if changed
               else "pinned" if details["reapplied_messages"] or details["summary_reused"] else "passthrough")
    info = CondenseInfo(**details, trigger=trigger, dropped_blocks=details["newly_dropped"],
                        session=key[:12], model=str(body.get("model", "")))
    usage = snapshot["usage"] if snapshot else 0  # zero means unavailable, NOT an estimate
    if messages is original and snapshot is None:
        return raw, info, usage, bool(body.get("stream"))
    body["messages"] = messages
    return json.dumps(body, ensure_ascii=False).encode("utf-8"), info, usage, bool(body.get("stream"))


def _record(info: CondenseInfo, usage: int) -> None:
    """Estimates describe prompts; only Pi's provider usage describes billed/cache tokens."""
    STATS.usage_tokens += usage
    STATS.passthrough += int(info.trigger == "passthrough")
    STATS.strategy_passes += bool(info.newly_dropped)
    STATS.dropped_blocks += info.newly_dropped
    STATS.trims += info.frontier_advanced
    STATS.frontier_reuses += bool(info.reapplied_messages or info.summary_reused)
    STATS.condenses += bool(info.condensed_entries)
    STATS.last_request = info
    if info.condensed_entries:
        STATS.last_condense = info


# --- inference host plumbing --------------------------------------------------------------
def _count(status: int) -> None:
    STATS.status[str(status)] = STATS.status.get(str(status), 0) + 1


def _tag(info: CondenseInfo, status: int) -> dict[str, str]:
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
             info: CondenseInfo) -> Response:
    """Mirror host status and bytes, tagging the trigger and counting the status."""
    status = upstream.status_code
    media_type = upstream.headers.get("content-type", "application/json")
    headers = _tag(info, status)
    return StreamingResponse(_chain(upstream, first, rest), status_code=status, headers=headers,
                             media_type=media_type)


async def proxy(provider: str, rest: str, request: Request, minify_body: bool) -> Response:
    """Forward one request, fitting managed chat messages before upstream dispatch."""
    base = ROUTES.get(provider)
    if not base:
        return JSONResponse({"error": {"message": f"unknown provider {provider}"}}, status_code=404)
    raw = await request.body()
    STATS.requests += 1
    body, info, streams = raw, CondenseInfo(), False
    if minify_body and len(raw) <= MAX_BODY_BYTES:
        try:
            body, info, usage, streams = await run_in_threadpool(minify, raw, dict(request.headers), provider)
        except utils.ContextOverflow as exc:
            STATS.guard_failures += 1
            _count(413)
            return JSONResponse({"error": {"message": str(exc)}}, status_code=413)
        except (ValueError, UnicodeError, TypeError, AttributeError):
            if request.headers.get("x-pi-ctx-session"):
                _count(400)
                return JSONResponse({"error": {"message": "Invalid managed chat request or context metadata"}}, status_code=400)
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


app = FastAPI(title="pi ctx engine", version="2.1", lifespan=lifespan)


@app.get("/health")
def health() -> Health:
    """Liveness of the condenser plus lifetime counters."""
    return Health(ok=True, usage_tokens=STATS.usage_tokens,
                  compactions=STATS.condenses, active=STATS.active, model=STATS.model)


@app.get("/stats")
def stats() -> Stats:
    """Every counter the pipeline mutates, per request."""
    return STATS


@app.get("/")
def root() -> dict:
    """Service identity: mode, budget and reachable endpoints."""
    return {"service": "pi-ctx-engine", "mode": LEVEL, "budget": BUDGET,
            "strategy": "durable session/model tool frontiers; overflow-only hysteresis; pinned summaries",
            "endpoints": ["/health", "/stats", "/r/{provider}/{rest}",
                          "/v1/chat/completions", "/codex/responses"]}


@app.post("/r/{provider}/{rest:path}")
async def route_provider(provider: str, rest: str, request: Request) -> Response:
    """Provider pass-through with minification."""
    return await proxy(provider, rest, request, minify_body=True)


@app.post("/v1/chat/completions")
async def route_sglang(request: Request) -> Response:
    """Local SGLang pass-through, never minified (it is the condenser itself)."""
    return await proxy("sglang", "chat/completions", request, minify_body=False)


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
    return _respond(upstream, first, chunks, CondenseInfo())


def serve() -> None:
    """Run the router as a standalone service."""
    uvicorn.run(app, host=HOST, port=PORT, log_level="info", access_log=False)
