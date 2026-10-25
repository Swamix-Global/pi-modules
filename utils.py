"""Strategy core: minify agent context for the inference host, condense via local SGLang.

Fast Mode (L1) drops tool calls and tool results before the current turn; Standard
Mode (L2) hands the surviving assistant/thinking history to the condenser. A turn
runs from a user message to its last successful assistant response, and tool blocks
always travel whole — a result without its call is rejected by every provider.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

import requests

# --- tunables -------------------------------------------------------------------------
DEFAULT_BUDGET = int(os.getenv("PI_ROUTER_BUDGET", 60_000))  # usage that triggers minification
CUT_FRACTION = float(os.getenv("PI_ROUTER_CUT_FRACTION", 0.4))  # cut target as a fraction of the budget
CONDENSE_TARGET_FRACTION = 0.2   # condenser target = this fraction of entry tokens
MSG_TOKEN_LIMIT = 40_000         # single messages above this are never dropped
CHARS_PER_TOKEN = 3.6            # conservative estimate, cap decisions only
MAX_BODY_BYTES = 128 << 20       # bodies above this are forwarded untouched
CONDENSE_RETRIES = 2
UPSTREAM_TIMEOUT = (10, 300)     # connect, read
GZIP_MIN_BYTES = 32 << 10        # gzip request bodies to cloud hosts above this
# uvicorn-backed locals cannot decode Content-Encoding; clouds decode at the edge
# Real cloud LLM APIs (DeepSeek, OpenRouter, Groq, etc.) reject Content-Encoding: gzip
# on POST /chat/completions with 400 'Failed to parse JSON'. Request gzip is only used
# when an inference host is explicitly listed in PI_ROUTER_GZIP_PROVIDERS.
GZIP_PROVIDERS = set(filter(None, os.getenv("PI_ROUTER_GZIP_PROVIDERS", "").split(",")))
MEDIA_TYPES = {"image", "image_url", "input_image", "file", "audio"}
THINKING_TYPES = {"thinking", "redacted_thinking", "reasoning"}  # preserved verbatim, never counted
MANAGED_AUTH = {"api-key", "authorization"}  # router injects its own bearer
CACHE_DIR = Path(os.getenv("PI_ROUTER_CACHE", "~/.pi/agent/ctx-cache")).expanduser()

SG_LANG_BASE = os.getenv("SG_LANG_BASE", "http://127.0.0.1:30000/v1")
SG_LANG_MODEL = os.getenv("SG_LANG_MODEL", "")  # default: whatever the host serves
AUTH_PATH = Path(os.getenv("PI_ROUTER_AUTH", "~/.pi/agent/auth.json")).expanduser()
ROUTES: dict[str, str] = json.loads(
    os.getenv("PI_ROUTER_ROUTES", json.dumps({
        "sglang": SG_LANG_BASE,
        "deepseek": "https://api.deepseek.com",
        "openrouter": "https://openrouter.ai/api/v1",
        "groq": "https://api.groq.com/openai/v1",
        "cerebras": "https://api.cerebras.ai/v1",
        "qwen-token-plan": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
        "xiaomi-token-plan-sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
        "antigravity": "https://daily-cloudcode-pa.googleapis.com",
        "lmstudio": "http://127.0.0.1:1234/v1",
        "openai": "https://api.openai.com/v1",
        "zai-coding-cn": "https://api.z.ai/api/coding/paas/v4",
        "zai": "https://api.z.ai/api/coding/paas/v4",
    }))
)

SNAPSHOT_RE = re.compile(r"_pi_ctx::(\{.*\})", re.DOTALL)
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}


# --- usage snapshot ---------------------------------------------------------------------
def parse_snapshot(text: str) -> dict[str, Any]:
    """Split the raw body from the _pi_ctx usage snapshot Pi appends to it."""
    if not (found := SNAPSHOT_RE.search(text)):
        raise ValueError("no _pi_ctx snapshot marker in request body")
    try:
        data = json.loads(found.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"snapshot payload is not valid JSON: {exc}") from exc
    if not isinstance(usage := data.get("usage") or data.get("tokens"), int) or usage <= 0:
        raise ValueError("snapshot has no integer usage")
    return {"usage": usage, "body": json.loads(text[:found.start()]),
            "start": found.start(), "end": found.end()}


def token_estimate(text: str) -> int:
    """Chars-to-tokens estimate; only ever used to pick drop victims."""
    return max(1, int(len(text) / CHARS_PER_TOKEN)) if text else 0


def content_sig(message: dict) -> str:
    """Content hash that identifies an already-condensed entry across requests."""
    return hashlib.sha1(json.dumps(message.get("content"), sort_keys=True,
                                   ensure_ascii=False).encode()).hexdigest()[:12]


# --- turn anatomy -----------------------------------------------------------------------
def _has_tool_calls(message: dict) -> bool:
    """True for Pi blocks (toolCall) and OpenAI wire format (tool_calls)."""
    return bool(message.get("tool_calls")) or any(
        isinstance(block, dict) and block.get("type") == "toolCall"
        for block in message.get("content") or ())


def _is_media(block: Any) -> bool:
    """True for any inline media block that balloons or breaks context."""
    return isinstance(block, dict) and block.get("type") in MEDIA_TYPES


def _call_ids_of(head: dict) -> set:
    """Every tool-call id a turn head announces, in either wire format."""
    announced = {block.get("id") for block in head.get("content") or ()
                 if isinstance(block, dict) and block.get("type") == "toolCall"}
    return (announced | {call.get("id") for call in head.get("tool_calls") or ()}) - {None}


def _blocks(messages: list[dict]) -> list[list[dict]]:
    """Segment into atomic blocks: a call turn absorbs exactly the results it owns."""
    blocks: list[list[dict]] = []
    for i, message in enumerate(messages):
        owner = blocks[-1] if blocks else None
        if (i and owner and owner[-1] is messages[i - 1]
                and message.get("role") in ("toolResult", "tool")
                and (message.get("toolCallId") or message.get("tool_call_id")) in _call_ids_of(owner[0])):
            owner.append(message)
        else:
            blocks.append([message])
    return blocks


def _droppable(block: list[dict]) -> bool:
    """Orphan results and assistant call turns are both safe to cut."""
    return block[0].get("role") in ("toolResult", "tool") or (
        block[0].get("role") == "assistant" and _has_tool_calls(block[0]))


def count_tool_blocks(messages: list[dict]) -> int:
    """Droppable blocks: assistant call turns plus tool results, either wire format."""
    return sum(1 for m in messages if m.get("role") in ("toolResult", "tool")
               or (m.get("role") == "assistant" and _has_tool_calls(m)))


def message_tokens(message: dict) -> int:
    """Estimated tokens, ignoring encrypted thinking/reasoning blobs."""
    content = message.get("content")
    body = token_estimate(content) if isinstance(content, str) else sum(
        token_estimate(json.dumps(block, ensure_ascii=False)) for block in content or ()
        if isinstance(block, dict) and block.get("type") not in THINKING_TYPES)
    return body + sum(token_estimate(json.dumps(call, ensure_ascii=False))
                      for call in message.get("tool_calls") or ())


def overhead_tokens(messages: list[dict]) -> int:
    """Estimated tokens across the whole conversation."""
    return sum(map(message_tokens, messages))


def strip_media(messages: list[dict]) -> tuple[list[dict], int]:
    """Drop media blocks before the current turn; screenshots balloon, providers reject."""
    cut = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    out = [m if i >= cut or not isinstance(content := m.get("content"), list)
              or not any(map(_is_media, content))
           else {**m, "content": [block for block in content if not _is_media(block)]}
           for i, m in enumerate(messages)]
    return out, sum(len(m["content"]) - len(k["content"]) for m, k in zip(messages, out) if m is not k)


# --- Fast Mode | L1 ----------------------------------------------------------------------
def passes(messages: list[dict], budget: int, low: int | None = None,
           usage: int | None = None) -> tuple[list, list, list]:
    """Greedily cut oldest whole tool blocks until usage falls to the low-water mark.

    Cutting exactly to the budget leaves no headroom: ordinary growth re-crosses
    the budget within a turn or two, so the drop frontier advances on nearly every
    request. Every advance deletes messages at the head, which diverges the prompt
    prefix at that point and forces the inference host to re-bill the whole tail as
    fresh input (observed: 11.5k cached = system prompt only, plus ~27.7k re-billed
    on 82% of requests). Cutting down to `low` instead keeps the frontier fixed for
    many turns: one cache rebuild per band rather than one per request.
    """
    over = (usage if usage is not None else overhead_tokens(messages)) - (budget if low is None else low)
    cut: list[dict] = []
    kept: list[dict] = []
    for block in _blocks(messages):
        sizes = [*map(message_tokens, block)]
        if over > 0 and max(sizes) <= MSG_TOKEN_LIMIT and _droppable(block):
            over -= sum(sizes)
            cut += block
        else:
            kept += block
    return cut, kept, cut[:2]


def repair_tool_pairs(messages: list[dict]) -> list[dict]:
    """Drop results whose call was cut and calls whose results all went with it.

    Any cut that is not block-aligned leaves an orphan `tool` message, and providers reject
    the whole request (`messages with role 'tool' must be a response to a preceding message
    with 'tool_calls'`). Both wire formats are handled, and the input list is returned as-is
    when nothing needs repair so an untouched request keeps its exact bytes.
    """
    live: dict[str, int] = {}
    out: list[dict] = []
    changed = False
    for message in messages:
        if ids := _call_ids_of(message):
            live = dict.fromkeys(ids)
            out.append(message)
            continue
        if (rid := message.get("toolCallId") or message.get("tool_call_id")) is not None:
            if rid in live:
                out.append(message)
            else:
                changed = True
            continue
        out.append(message)
    answered = {m.get("toolCallId") or m.get("tool_call_id") for m in out}
    if any(ids and not (ids & answered) for ids in map(_call_ids_of, out)):
        out = [m for m in out if (ids := _call_ids_of(m)) == set() or (ids & answered)]
        changed = True
    return out if changed else messages


def _anchor(messages: list[dict]) -> int:
    """Leading system messages plus the first user turn: the task header, never trimmed."""
    i = 0
    while i < len(messages) and messages[i].get("role") == "system":
        i += 1
    if i < len(messages) and messages[i].get("role") == "user":
        i += 1
    return i


def _turn_start(messages: list[dict]) -> int:
    """Index of the current turn: everything from here on stays."""
    return max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=len(messages))


def message_sig(message: dict) -> str:
    """Stable identity of one message, used to validate a pinned cut boundary."""
    body = {"role": message.get("role"),
            "id": message.get("tool_call_id") or message.get("toolCallId"),
            "content": message.get("content"),
            "calls": [call.get("id") for call in message.get("tool_calls") or ()]}
    return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


def cut_prefix(messages: list[dict], budget: int, low: int,
               anchor: int | None = None) -> tuple[list[dict], int]:
    """Drop a contiguous, block-aligned prefix of old turns until the payload fits `low`.

    A prefix cut is what keeps the prompt stable: whatever survives starts at the same point
    next request, so the host still has it cached. Cutting scattered blocks out of the middle
    would shift the head every time and re-bill the tail instead.
    """
    if overhead_tokens(messages) <= budget:
        return messages, 0
    anchor = _anchor(messages) if anchor is None else anchor
    turn = _turn_start(messages)
    over = overhead_tokens(messages) - low
    dropped = 0
    for block in _blocks(messages[anchor:turn]):
        if over <= 0:
            break
        over -= sum(map(message_tokens, block))
        dropped += len(block)
    if not dropped:
        return messages, 0
    return messages[:anchor] + messages[anchor + dropped:], dropped


# --- L1 frontier pinning ------------------------------------------------------------------
FRONTIER_CAP = 64  # remembered conversations; least recently used is evicted


class Frontier:
    """Per-conversation drop point that stays fixed until the payload outgrows the budget.

    Clients resend the whole conversation every request, so a cut recomputed from scratch
    walks forward as the tail grows: the prompt head changes on nearly every request and the
    host has to re-bill the whole tail, which is what the cache-miss notices report. Pinning
    the drop point keeps the head byte-identical between requests, so only the new turn is
    fresh input; the frontier moves once per budget band instead of once per request.
    """

    def __init__(self, cap: int = FRONTIER_CAP) -> None:
        self._pins: OrderedDict[str, tuple[int, str]] = OrderedDict()
        self._cap = cap

    @staticmethod
    def key(messages: list[dict]) -> str:
        """Conversation identity: the system prompt plus the first turn of the transcript."""
        seed = next((m for m in messages if m.get("role") != "system"), {})
        return hashlib.sha1((json.dumps(messages[0] if messages else {}, sort_keys=True)
                             + "|" + content_sig(seed)).encode()).hexdigest()[:16]

    def _remember(self, key: str, pin: tuple[int, str]) -> None:
        """Store the pin, evicting the least recently used conversation."""
        self._pins[key] = pin
        self._pins.move_to_end(key)
        while len(self._pins) > self._cap:
            self._pins.popitem(last=False)

    def apply(self, messages: list[dict], budget: int, low: int) -> tuple[list[dict], int]:
        """Return the pinned view of one conversation, cutting only when it outgrows `budget`."""
        if not messages:
            return messages, 0
        key = self.key(messages)
        anchor = _anchor(messages)
        start, sig = self._pins.get(key, (anchor, ""))
        if start < anchor or start >= len(messages) or (sig and message_sig(messages[start - 1]) != sig):
            start, sig = anchor, ""  # unknown or rewritten history: fall back to a fresh cut
        head, view = messages[:anchor], messages[start:]
        if overhead_tokens(head) + overhead_tokens(view) <= budget:
            self._remember(key, (start, sig))
            return (head + repair_tool_pairs(view)) if start > anchor else messages, 0
        kept, dropped = cut_prefix(messages, budget, low, anchor)
        if not dropped:
            return messages, 0  # nothing cuttable before the current turn: keep the head
        boundary = anchor + dropped
        self._remember(key, (boundary, message_sig(messages[boundary - 1])))
        return repair_tool_pairs(kept), count_tool_blocks(messages[anchor:boundary])


FRONTIER = Frontier()


def drop_blocks_before_current_turn(messages: list[dict], budget: int,
                                    usage: int | None = None) -> tuple[list, int]:
    """L1: strip media, then cut whole tool blocks before the current turn.

    `usage` is accepted for call-site compatibility but never drives the decision: the client
    snapshot counts the history before this router trimmed it, so it stays above the budget
    for the rest of the session and would re-trim on every single request.
    """
    stripped, _ = strip_media(messages)
    return FRONTIER.apply(stripped, budget, int(budget * CUT_FRACTION))


# --- Standard Mode | L2 ------------------------------------------------------------------
CONDENSE_PROMPT = (
    "You compress a working-memory dump from a coding agent session. "
    "Preserve: file paths, function and variable names, commands, errors, "
    "decisions, open questions, task state. Drop: tool noise, retries, "
    "superseded attempts. Output only the compressed notes, no preamble."
)


def condensed_prefix_build(messages: list[dict], prev_entry_sigs: list[str]) -> tuple[list, list]:
    """Entries the condenser may fold: pre-turn, non-system, and never the same twice."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    entries = [m for m in messages[:last_user] if m.get("role") != "system"] if last_user > 0 else []
    if not entries and len(messages) >= 2 and (
            messages[-2].get("role") == "assistant" and _has_tool_calls(messages[-2])
            and messages[-1].get("role") in ("toolResult", "tool")):
        entries = messages[-2:]  # single-turn session: one giant tool dump still condenses
    sigs = [*map(content_sig, entries)]
    return ([], prev_entry_sigs) if entries and sigs == prev_entry_sigs else (entries, sigs)


def context_messages(messages: list[dict], budget: int) -> list[dict]:
    """Trim to the newest whole turns within budget, never rewriting a single byte."""
    split = next((i for i, m in enumerate(messages) if m.get("role") != "system"), len(messages))
    head, rest = messages[:split], messages[split:]
    turns: list[list[dict]] = []
    for m in rest:
        turns.append([m]) if m.get("role") == "user" or not turns else turns[-1].append(m)
    sizes = [*map(overhead_tokens, turns)]
    start, total = len(turns), 0
    while start and (start == len(turns) or total + sizes[start - 1] <= budget):
        start, total = start - 1, total + sizes[start - 1]
    return head + [m for turn in turns[start:] for m in turn]


def local_ai(system_prompt: str, user_message: str, max_tokens: int = 2048) -> str:
    """Ask the local granite model; durable file cache keyed by the input hash."""
    key = hashlib.sha1(json.dumps([system_prompt, user_message, max_tokens],
                                  ensure_ascii=False).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.txt"
    if path.exists():
        return path.read_text()
    payload = {"model": SG_LANG_MODEL or _served_model(), "max_tokens": max_tokens,
               "temperature": 0.2, "chat_template_kwargs": {"enable_thinking": False},
               "messages": [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message}]}
    last: Exception | None = None
    for _ in range(CONDENSE_RETRIES + 1):
        try:
            resp = requests.post(f"{SG_LANG_BASE}/chat/completions", json=payload, timeout=(5, 120))
            resp.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text := resp.json()["choices"][0]["message"]["content"].strip())
            return text
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            last = exc
    raise RuntimeError(f"local_ai failed after {CONDENSE_RETRIES + 1} attempts: {last}")


def _format_calls(message: dict) -> str:
    """Render OpenAI wire tool_calls as condenser lines."""
    role = message.get("role", "?")
    return "\n".join(f"[{role} toolCall {(fn := call.get('function') or {}).get('name', '?')}]"
                     f" {fn.get('arguments', '')}"
                     for call in message.get("tool_calls") or ())


def _format_block(role: str, block: dict) -> str:
    """Render one content block as a condenser line, unknown types truncated."""
    kind = block.get("type", "?")
    if kind == "text":
        return f"[{role}] {block.get('text', '')}"
    if kind == "toolCall":
        return f"[{role} toolCall {block.get('name', '?')}] {json.dumps(block.get('arguments', {}), ensure_ascii=False)}"
    if kind == "image":
        return f"[{role} image omitted]"
    return f"[{role} {kind}] {json.dumps(block, ensure_ascii=False)[:400]}"


def _format_entry(message: dict) -> str:
    """Flatten one message into condenser-readable text."""
    role, content = message.get("role", "?"), message.get("content")
    lines = ([f"[{role}] {content}"] if isinstance(content, str) else
             [_format_block(role, block) for block in content or () if isinstance(block, dict)])
    return "\n".join(filter(None, [_format_calls(message), *lines]))


def condense(entries: list[dict], target_tokens: int) -> tuple[str, float]:
    """Fold entries into notes through local_ai; returns (notes, elapsed ms)."""
    chars = int(target_tokens * CHARS_PER_TOKEN)
    started = time.perf_counter()
    text = local_ai(f"{CONDENSE_PROMPT} Keep it under ~{chars} characters.",
                    "\n\n".join(filter(None, map(_format_entry, entries))),
                    max_tokens=max(256, min(4096, int(chars / 3.2))))
    return text, (time.perf_counter() - started) * 1000


def _served_model() -> str:
    """Served model id of the local host, or 'default' when it is down."""
    try:
        return requests.get(f"{SG_LANG_BASE}/models", timeout=(3, 10)).json()["data"][0]["id"]
    except Exception:
        return "default"


# --- inference host plumbing --------------------------------------------------------------
def auth_map() -> dict[str, str]:
    """provider -> bearer key from auth.json; missing file or entries are fine."""
    try:
        raw = json.loads(AUTH_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return {provider: key for provider, entry in raw.items() if isinstance(entry, dict)
            and isinstance(key := entry.get("key") or entry.get("apiKey") or entry.get("access"), str) and key}


def upstream_headers(provider: str, headers, auth: dict[str, str]) -> dict[str, str]:
    """Strip hop headers; managed providers get router-injected auth, not client auth."""
    out = {k: v for k, v in headers.items() if k.lower() not in HOP_HEADERS}
    if not any(k.lower() == "content-type" for k in out):
        out["Content-Type"] = "application/json"
    if not any(k.lower() == "accept-encoding" for k in out):
        out["Accept-Encoding"] = "gzip, deflate"
    return ({**{k: v for k, v in out.items() if k.lower() not in MANAGED_AUTH},
             "Authorization": f"Bearer {auth[provider]}"} if provider in auth else out)


class UpstreamError(RuntimeError):
    """Inference host answered with an unusable status."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def maybe_gzip(provider: str, body: bytes, headers: dict[str, str]) -> bytes:
    """Gzip big cloud-bound bodies; edges decode them, local hosts cannot."""
    if provider in GZIP_PROVIDERS and len(body) >= GZIP_MIN_BYTES:
        headers["Content-Encoding"], body = "gzip", gzip.compress(body)
    return body
