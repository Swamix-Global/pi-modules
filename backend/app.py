"""pi ctx engine — minifies agent context before the inference host sees it.

A turn is a user message through its last successful assistant response. Fast Mode (L1)
cuts whole tool blocks before the current turn; Standard Mode (L2) then folds the surviving
history through the local condenser. Usage is never taken from a request parameter — it
comes from the `_pi_ctx` snapshot Pi appends to the body, which is also why a request the
router does not have to touch is forwarded byte-for-byte: identical bytes are a cache hit.
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager

import requests
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from . import condenser, strategy
from .config import (BUDGET, CODEX_HOST, CONDENSE_TARGET_FRACTION, CUT_FRACTION, HOST,
                     LEVEL, MAX_BODY_BYTES, PORT, ROUTES, SG_LANG_BASE, UPSTREAM_TIMEOUT)
from .transport import auth_map, maybe_gzip, parse_snapshot, upstream_headers


# --- shapes -------------------------------------------------------------------------------
class CondenseInfo(BaseModel):
    """What one request lost: dropped blocks, folded entries, trigger, cost."""

    dropped_blocks: int = 0
    condensed_entries: int = 0
    summary_chars: int = 0
    duration_ms: float = 0.0
    trigger: str = "passthrough"  # passthrough | strategy | strategy+condenser


class Stats(BaseModel):
    """`/stats` counters, mutated once per request."""

    requests: int = 0
    passthrough: int = 0
    strategy_passes: int = 0
    condenses: int = 0
    dropped_blocks: int = 0
    trims: int = 0            # requests whose drop frontier moved (each rebuilds the cache)
    rebuild_tokens: int = 0   # estimated tokens re-billed by those moves
    usage_tokens: int = 0
    status: dict[str, int] = Field(default_factory=dict)
    last_condense: CondenseInfo | None = None
    frontier_pins: int = 0    # conversations whose drop point is currently pinned
    frontier_resets: int = 0  # pins that could not be relocated (history was rewritten)
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
NOTE_HEADER = "[context notes — earliest turns condensed]"
_prev_sigs: list[str] = []  # entry signatures already folded into a note this session


# --- minification pipeline ----------------------------------------------------------------
def _fold(messages: list[dict], entries: list[dict], sigs: list[str]) -> tuple[list[dict], dict]:
    """L2 compression: fold the oldest entries into one leading note, fail open."""
    try:
        target = max(512, int(strategy.overhead_tokens(entries) * CONDENSE_TARGET_FRACTION))
        summary, ms = condenser.condense(entries, target)
    except (RuntimeError, requests.RequestException):
        return messages, {}  # condenser down: ship the turn untouched rather than fail
    at = messages.index(entries[0])
    _prev_sigs[:] = sigs  # identical entries never condense twice
    return ([*messages[:at], {"role": "user", "content": f"{NOTE_HEADER}\n\n{summary}"},
             *messages[at + len(entries):]],
            {"condensed_entries": len(entries), "summary_chars": len(summary),
             "duration_ms": round(ms, 1), "trigger": "strategy+condenser"})


def _l1(messages: list[dict]) -> tuple[list[dict], int]:
    """Drop whole pre-turn tool blocks, counting the cache rebuild each move costs."""
    trimmed, dropped = strategy.drop_blocks_before_current_turn(messages, BUDGET)
    if dropped:
        STATS.trims += 1
        STATS.rebuild_tokens += strategy.overhead_tokens(trimmed)
    return trimmed, dropped


def _l2(messages: list[dict], info: CondenseInfo) -> tuple[list[dict], CondenseInfo]:
    """Fold history through the condenser when L1 alone did not fit the budget."""
    entries, sigs = strategy.condensed_prefix_build(messages, _prev_sigs)
    if not entries:
        return messages, info
    folded, patch = _fold(messages, entries, sigs)
    return folded, info.model_copy(update=patch) if patch else info


def minify(raw: bytes) -> tuple[bytes, CondenseInfo, int, bool]:
    """Minify one raw body; returns (body, telemetry, usage, streams)."""
    text = raw.decode("utf-8", errors="replace")
    snapshot = None
    try:
        snapshot = parse_snapshot(text)
        usage, body = snapshot["usage"], snapshot["body"]
    except ValueError:
        body = json.loads(text)  # snapshot-free client: usage is estimated
        usage = strategy.overhead_tokens(body.get("messages") or [])

    original = body.get("messages")
    if not isinstance(original, list) or not original:
        raise ValueError("no messages to minify")
    streams = bool(body.get("stream"))
    info, messages = CondenseInfo(), original
    if strategy.overhead_tokens(messages) > BUDGET and not all(
            m.get("role") == "system" for m in messages):
        messages, info.dropped_blocks = _l1(messages)
        if LEVEL == "L2" and strategy.overhead_tokens(messages) > BUDGET:
            messages, info = _l2(messages, info)
    # Provider-reported usage is the only authoritative size signal. Acting on the char
    # estimate while a snapshot exists trims messages the host had already cached, so the
    # fallback trim is limited to snapshot-free clients that give us no usage at all.
    if snapshot is None and strategy.overhead_tokens(messages) > BUDGET:
        messages = strategy.context_messages(messages, int(BUDGET * CUT_FRACTION))
    if messages is original:
        if snapshot is None:
            return raw, info, usage, streams  # nothing to do: forward verbatim
    else:
        messages = strategy.repair_tool_pairs(messages)  # a cut may have orphaned a result
        body["messages"] = messages
    # the _pi_ctx marker is router metadata: it never travels to the inference host
    return json.dumps(body, ensure_ascii=False).encode("utf-8"), info, usage, streams


def _record(info: CondenseInfo, usage: int) -> None:
    """Lifetime counters for one minified request."""
    STATS.usage_tokens += usage
    STATS.passthrough += int(info.trigger == "passthrough")
    STATS.strategy_passes += bool(info.dropped_blocks)
    STATS.dropped_blocks += info.dropped_blocks
    STATS.condenses += bool(info.condensed_entries)
    if info.condensed_entries:
        STATS.last_condense = info


# --- inference host plumbing --------------------------------------------------------------
def _count(status: int) -> None:
    STATS.status[str(status)] = STATS.status.get(str(status), 0) + 1


def _tag(info: CondenseInfo, status: int) -> dict[str, str]:
    _count(status)
    return {"x-pi-router": info.trigger}


async def _chain(first: bytes, rest):
    """Yield the already-read head, then every live host chunk."""
    yield first
    for chunk in rest:
        yield chunk


def _respond(upstream: requests.Response, first: bytes, rest,
             info: CondenseInfo) -> Response:
    """Mirror host status and bytes, tagging the trigger and counting the status."""
    status = upstream.status_code
    media_type = upstream.headers.get("content-type", "application/json")
    headers = _tag(info, status)
    if status >= 400:
        return Response(content=first, status_code=status, headers=headers,
                        media_type=media_type)
    return StreamingResponse(_chain(first, rest), status_code=status, headers=headers,
                             media_type=media_type)


async def proxy(provider: str, rest: str, request: Request, minify_body: bool) -> Response:
    """Forward one request, minifying it whenever it carries a usage snapshot."""
    base = ROUTES.get(provider)
    if not base:
        return JSONResponse({"error": {"message": f"unknown provider {provider}"}}, status_code=404)
    raw = await request.body()
    STATS.requests += 1
    body, info, streams = raw, CondenseInfo(), False
    if minify_body and len(raw) <= MAX_BODY_BYTES:
        try:
            body, info, usage, streams = minify(raw)
        except ValueError:
            pass  # plain clients (lmstudio, curl) pass through untouched
        else:
            _record(info, usage)
    headers = upstream_headers(provider, dict(request.headers), auth_map())
    try:
        upstream = SESSION.post(f"{base.rstrip('/')}/{rest}", headers=headers, stream=True,
                                data=maybe_gzip(provider, body, headers),
                                timeout=UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        _count(502)
        return JSONResponse({"error": {"message": f"inference host {provider}: {exc}"}},
                            status_code=502)
    chunks = upstream.iter_content(65536)
    first = next(chunks, b"")
    if not minify_body or streams:
        return _respond(upstream, first, chunks, info)
    payload = first + b"".join(chunks)  # non-streaming clients expect one body
    return Response(payload, status_code=upstream.status_code,
                    headers=_tag(info, upstream.status_code),
                    media_type=upstream.headers.get("content-type", "application/json"))


def _ping() -> None:
    """Refresh served-model name and liveness of the local SGLang host."""
    model = condenser.served_model()
    STATS.active = model != "default"
    STATS.model = model if STATS.active else STATS.model


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Ping the local host once so /health is meaningful at boot."""
    _ping()
    yield


app = FastAPI(title="pi ctx engine", version="2.1", lifespan=lifespan)


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
    STATS.frontier_pins = strategy.FRONTIER.size
    STATS.frontier_resets = strategy.FRONTIER.resets
    return STATS


@app.get("/")
def root() -> dict:
    """Service identity: mode, budget and reachable endpoints."""
    return {"service": "pi-ctx-engine", "mode": LEVEL, "budget": BUDGET,
            "strategy": "pin a whole-block drop frontier before the current turn",
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
    auth = auth_map().get("openai-codex")
    headers = upstream_headers("openai-codex", dict(request.headers),
                               {"openai-codex": auth} if auth else {})
    try:
        upstream = SESSION.post(CODEX_HOST, data=raw, headers=headers, stream=True,
                                timeout=UPSTREAM_TIMEOUT)
    except requests.RequestException as exc:
        _count(502)
        return JSONResponse({"error": {"message": f"codex host: {exc}"}}, status_code=502)
    chunks = upstream.iter_content(65536)
    return _respond(upstream, next(chunks, b""), chunks, CondenseInfo())


def serve() -> None:
    """Run the router as a standalone service."""
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
