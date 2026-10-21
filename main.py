"""pi ctx engine — 4141. Minifies agent context before the inference host sees it.

A turn is a user message through its last successful assistant response. Fast Mode
(L1) cuts tool calls and tool results before the current turn; Standard Mode (L2)
then condenses assistant/thinking history through the local SGLang granite model.
Usage is never taken from a request parameter — it comes from the _pi_ctx snapshot
Pi appends to the body, which keeps the provider prefix cache pristine.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import utils as u
from models import CondenseInfo, Health, Stats

STATS, PREV_ENTRY_SIGS, SESSION = Stats(), [], requests.Session()
BUDGET = u.DEFAULT_BUDGET
LEVEL = os.getenv("PI_ROUTER_LEVEL", "L2")  # Fast Mode | L1 vs Standard Mode | L2
NOTE_HEADER = "[context notes — earliest turns condensed]"
CODEX_HOST = os.getenv("PI_ROUTER_CODEX_UPSTREAM", "https://chatgpt.com/backend-api/codex/responses")


# --- minification pipeline ------------------------------------------------------------------
def _fold(messages: list[dict], entries: list[dict], sigs: list[str]) -> tuple[list[dict], dict]:
    """L2 compression: fold the oldest entries into one leading note, fail-open."""
    try:
        summary, ms = u.condense(entries, max(512, int(u.overhead_tokens(entries)
                                                     * u.CONDENSE_TARGET_FRACTION)))
    except (RuntimeError, requests.RequestException):
        return messages, {}  # condenser down: ship the turn untouched rather than fail
    at = messages.index(entries[0])
    PREV_ENTRY_SIGS[:] = sigs  # identical entries never condense twice
    return ([*messages[:at], {"role": "user", "content": f"{NOTE_HEADER}\n\n{summary}"},
             *messages[at + len(entries):]],
            {"condensed_entries": len(entries), "summary_chars": len(summary),
             "duration_ms": round(ms, 1), "trigger": "strategy+condenser"})


def compact_payload(raw: bytes) -> tuple[bytes, CondenseInfo, int, bool]:
    """Minify one raw body: L1 drops, L2 condense, then a byte-preserving safety trim."""
    text = raw.decode("utf-8", errors="replace")
    try:
        snap = u.parse_snapshot(text)
        usage, body = snap["usage"], snap["body"]
    except ValueError:
        snap, body = None, json.loads(text)  # snapshot-free client: usage is estimated
        usage = u.overhead_tokens(body.get("messages") or [])
    if not isinstance(messages := body.get("messages"), list) or not messages:
        raise ValueError("no messages to minify")
    original, info = messages, CondenseInfo()
    if usage > BUDGET and not all(m.get("role") == "system" for m in messages):
        before = u.overhead_tokens(messages)
        messages, info.dropped_blocks = u.drop_blocks_before_current_turn(messages, BUDGET, usage=usage)
        if info.dropped_blocks:
            info.trigger = "strategy"
        if LEVEL == "L2" and usage - (before - u.overhead_tokens(messages)) > BUDGET:
            entries, sigs = u.condensed_prefix_build(messages, PREV_ENTRY_SIGS)
            if entries:
                messages, patch = _fold(messages, entries, sigs)
                info = info.model_copy(update=patch) if patch else info
    if u.overhead_tokens(messages) > BUDGET:
        messages = u.context_messages(messages, int(BUDGET * 1.3))
    if messages is not original:
        body["messages"] = messages
    elif snap is None:
        return raw, info, usage, bool(body.get("stream"))  # nothing to do: forward verbatim
    # the _pi_ctx marker is router metadata: it never travels to the inference host
    return json.dumps(body, ensure_ascii=False).encode("utf-8"), info, usage, bool(body.get("stream"))


# --- inference host plumbing -----------------------------------------------------------------
def _respond(upstream: requests.Response, first: bytes, rest, info: CondenseInfo,
             status: int | None = None) -> Response:
    """Mirror host status and bytes, tagging the trigger and counting the status."""
    status = status or upstream.status_code
    STATS.status[str(status)] = STATS.status.get(str(status), 0) + 1
    headers = {"x-pi-router": info.trigger}
    if status >= 400:
        return Response(content=first, status_code=status, headers=headers,
                        media_type=upstream.headers.get("content-type", "text/plain"))
    return StreamingResponse(_chain(first, rest), status_code=status, headers=headers,
                             media_type=upstream.headers.get("content-type", "application/json"))


async def _chain(first: bytes, rest):
    """Yield the already-read head, then every live host chunk."""
    yield first
    for chunk in rest:
        yield chunk


async def proxy(provider: str, rest: str, request: Request, minify: bool) -> Response:
    """Forward one request, minifying it whenever it carries a usage snapshot."""
    if not (base := u.ROUTES.get(provider)):
        return JSONResponse({"error": {"message": f"unknown provider {provider}"}}, status_code=404)
    raw = await request.body()
    STATS.requests += 1
    body, info, stream = raw, CondenseInfo(), False
    if minify and len(raw) <= u.MAX_BODY_BYTES:
        try:
            body, info, usage, stream = compact_payload(raw)
        except ValueError:
            pass  # no snapshot: plain clients (lmstudio, curl) pass through untouched
        else:
            STATS.usage_tokens += usage
            STATS.passthrough += int(info.trigger == "passthrough")
            STATS.strategy_passes += bool(info.dropped_blocks)
            STATS.dropped_blocks += info.dropped_blocks
            STATS.condenses += bool(info.condensed_entries)
            STATS.last_condense = info if info.condensed_entries else STATS.last_condense
    headers = u.upstream_headers(provider, dict(request.headers), u.auth_map())
    try:
        upstream = SESSION.post(f"{base.rstrip('/')}/{rest}", headers=headers, stream=True,
                                data=u.maybe_gzip(provider, body, headers), timeout=u.UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        STATS.status["502"] = STATS.status.get("502", 0) + 1
        return JSONResponse({"error": {"message": f"inference host {provider}: {exc}"}}, status_code=502)
    chunks = upstream.iter_content(65536)
    first = next(chunks, b"")
    if not minify or stream:
        return _respond(upstream, first, chunks, info)
    payload = first + b"".join(chunks)  # non-streaming clients expect one body
    STATS.status[str(upstream.status_code)] = STATS.status.get(str(upstream.status_code), 0) + 1
    return Response(payload, status_code=upstream.status_code, headers={"x-pi-router": info.trigger},
                    media_type=upstream.headers.get("content-type", "application/json"))


# --- app --------------------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ping the local host once so /health is meaningful at boot."""
    _ping()
    yield


def _ping() -> None:
    """Refresh served-model name and liveness of the local SGLang host."""
    try:
        STATS.model, STATS.active = requests.get(
            f"{u.SG_LANG_BASE}/models", timeout=(3, 10)).json()["data"][0]["id"], True
    except Exception:
        STATS.active = False


app = FastAPI(title="pi ctx engine", version="2.0", lifespan=lifespan)


@app.get("/health")
def health() -> Health:
    """Liveness of the condenser plus lifetime counters."""
    _ping()
    return Health(ok=STATS.active, usage_tokens=STATS.usage_tokens,
                  compactions=STATS.condenses, active=STATS.active, model=STATS.model)


@app.get("/stats")
def stats() -> Stats:
    """Every counter the pipeline mutates, per request."""
    _ping()
    return STATS


@app.get("/")
def root() -> dict:
    """Service identity: mode, budget and reachable endpoints."""
    return {"service": "pi-ctx-engine", "mode": LEVEL, "budget": BUDGET,
            "strategy": "drop whole tool blocks before the current turn",
            "endpoints": ["/health", "/stats", "/r/{provider}/{rest}",
                          "/v1/chat/completions", "/codex/responses"]}


@app.post("/r/{provider}/{rest:path}")
async def route_provider(provider: str, rest: str, request: Request) -> Response:
    """Provider pass-through with minification."""
    return await proxy(provider, rest, request, minify=True)


@app.post("/v1/chat/completions")
async def route_sglang(request: Request) -> Response:
    """Local SGLang pass-through, never minified (it is the condenser itself)."""
    return await proxy("sglang", "chat/completions", request, minify=False)


@app.post("/codex/responses")
async def route_codex(request: Request) -> Response:
    """Codex responses forwarded verbatim to the ChatGPT backend."""
    raw = await request.body()
    STATS.requests += 1
    auth = u.auth_map().get("openai-codex")
    headers = u.upstream_headers("openai-codex", dict(request.headers),
                                {"openai-codex": auth} if auth else {})
    try:
        upstream = SESSION.post(CODEX_HOST, data=raw, headers=headers, stream=True,
                                timeout=u.UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        return JSONResponse({"error": {"message": f"codex host: {exc}"}}, status_code=502)
    chunks = upstream.iter_content(65536)
    return _respond(upstream, next(chunks, b""), chunks, CondenseInfo())


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=int(os.getenv("PI_ROUTER_PORT", "4141")),
                log_level="info")
