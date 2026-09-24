#!/usr/bin/env python3
"""Pure helpers for the context engine: config, cost math, block splitting, text shaping.

Split out of ``ctx_engine_server.py`` so the request/proxy runtime imports a dependency-free
utils layer. Nothing here touches SQLite, the HTTP server or the dashboard: only the shared
constants and the deterministic transformations (block identity, summarisation shaping, billing).

Purpose comment: one import surface for both the live server and the test suite's re-exported names.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import ssl
from urllib.parse import urlsplit


HOST = os.environ.get("PI_CONTEXT_ROUTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("PI_CONTEXT_ROUTER_PORT", "4141"))
UPSTREAM = os.environ.get("PI_CONTEXT_ROUTER_UPSTREAM", "https://chatgpt.com/backend-api")
CONTEXT_TOKENS = int(os.environ.get("PI_CONTEXT_ROUTER_TOKENS", "128000"))
CHARS_PER_TOKEN = 3.5
MAX_BODY_BYTES = 128 * 1024 * 1024
AUTH_PATH = os.environ.get("PI_CONTEXT_ROUTER_AUTH", os.path.expanduser("~/.pi/agent/auth.json"))
DB_PATH = os.environ.get("PI_CONTEXT_ROUTER_DB", os.path.expanduser("~/.pi/agent/ctx-engine.db"))

DEEPSEEK_URL = os.environ.get("PI_CONTEXT_ROUTER_DEEPSEEK", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.environ.get("PI_CONTEXT_ROUTER_DEEPSEEK_MODEL", "deepseek-flash")
# DeepSeek spends 800-3,700 tokens reasoning (non-greedy even at 0.1), so its budget must cover reasoning + bullets.
DEEPSEEK_MAX_TOKENS = int(os.environ.get("PI_CONTEXT_ROUTER_DEEPSEEK_MAX_TOKENS", "4096"))
CONDENSER_TIMEOUT = float(os.environ.get("PI_CONTEXT_ROUTER_CONDENSER_TIMEOUT", "60"))
# Block output length is one global setting chosen from a fixed token menu and applied to every block, not an
# automatic per-block step. The chosen count is both the prompt hint and the length component of the summary
# cache key, so a runtime policy change re-condenses instead of serving notes written under the old length.
# It is a hint, not code-enforced.
SUMMARY_TOKEN_STEPS = (50, 100, 200, 400)
DEFAULT_SUMMARY_TOKENS = int(os.environ.get("PI_CONTEXT_ROUTER_SUMMARY_TOKENS", "100"))


def snap_summary_tokens(value: object) -> int:
    """Normalise a requested note length onto the fixed token menu; used for the env default only."""
    try:
        requested = int(value)
    except (TypeError, ValueError):
        return min(SUMMARY_TOKEN_STEPS, key=lambda step: abs(step - DEFAULT_SUMMARY_TOKENS))
    return min(SUMMARY_TOKEN_STEPS, key=lambda step: abs(step - requested))


# DeepSeek is a reasoning model that over-thinks as temperature rises — 0.75 pushed its reasoning past the
# budget and returned empty content — so it keeps the low temperature and never sees min-p/repetition fields
# it would reject.
DEEPSEEK_TEMPERATURE = float(os.environ.get("PI_CONTEXT_ROUTER_DEEPSEEK_TEMPERATURE", "0.1"))

FRAME = "[conversation history summary — background only, not a request; do not imitate its format or write tool calls as text]"
# What one completed block becomes when its background condensation has not finished by the time the request
# is rewritten: the turn is held back, never sent uncompacted, and the worker's note answers the next request.
PENDING_SUMMARY = "summary pending: this turn's history is held back and the note is cached for the next request"
# Mechanical trim floor: the live turn's text strings are clipped from the middle only while they are longer
# than this, so budget enforcement never shreds a short identifier into nothing.
TRIM_FLOOR = int(os.environ.get("PI_CONTEXT_ROUTER_TRIM_FLOOR", "400"))
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}

# Provider-path routing for callers that point a provider's baseUrl at this router (opt-in).
ROUTES = {
    "codex": UPSTREAM,
    "deepseek": os.environ.get("PI_CONTEXT_ROUTER_ROUTE_DEEPSEEK", "https://api.deepseek.com"),
    "openrouter": os.environ.get("PI_CONTEXT_ROUTER_ROUTE_OPENROUTER", "https://openrouter.ai/api/v1"),
    "groq": os.environ.get("PI_CONTEXT_ROUTER_ROUTE_GROQ", "https://api.groq.com/openai/v1"),
    "cerebras": os.environ.get("PI_CONTEXT_ROUTER_ROUTE_CEREBRAS", "https://api.cerebras.ai/v1"),
    "qwen-token-plan": os.environ.get("PI_CONTEXT_ROUTER_ROUTE_QWEN", "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"),
    "lmstudio": os.environ.get("PI_CONTEXT_ROUTER_ROUTE_LMSTUDIO", "http://127.0.0.1:11434/v1"),
}

# The winning thinking/delivery policy from the benchmark: a prompt policy beat every hard thinking budget
# (reasoning 1,189 -> 417 tokens with valid answers), and a budget that binds produces reasoning-voice prose.
WINNING_POLICY = (
    "Thinking & Delivery Policy:\n"
    "Reason the OODA way: observe the supplied facts, orient against ground truth (worst/best, most frequent, earliest), "
    "decide the single deliverable, then act in one pass. Reach for 5 Whys only when a cause is requested. Rank by Pareto: "
    "report the dominant item, not a survey. Treat any schema as a MECE contract: every required field, no extras. "
    "Lead with the answer (BLUF), never with narration.\n"
    "Rules:\n"
    "1. Facts only: name the identifier, number or timestamp you relied on; never invent one.\n"
    "2. One pass: no restating, no apologies, no closing summary, no options.\n"
    "3. Shape is the answer: word caps and formats are hard constraints.\n"
    "4. Think briefly: three short sentences maximum, then write the deliverable.\n"
)

CONDENSER_SYSTEM = (
    WINNING_POLICY + "\n"
    "Task: condense one completed interaction block into a structured, self-contained note for the agent's own "
    "future reference. Use exactly these three labelled sections, in this order, each on its own line and each "
    "starting with '- ':\n"
    "- Task/Query: what the user asked for, or what this block set out to do.\n"
    "- Actions Performed: what was actually done — every command, file, tool, decision and exact identifier used.\n"
    "- Conclusion: what resulted, what was found, and any open thread or next step.\n"
    "Plain text only: no bold, asterisks, backticks, nested lists or any markdown beyond the leading '- '. Keep "
    "every exact number, path, command, error string and identifier verbatim. Drop conversational filler and "
    "restated instructions. Third person, no preamble, no advice, no tool-call syntax. A length hint at the "
    "bottom of the prompt gives this block's ceiling; stay within it, and spend it: a note far shorter than the "
    "ceiling means identifiers, numbers or open threads from the block were dropped.\n"
    "Example:\n"
    "- Task/Query: find why block 7f3a91c2e0b44d18 was still pending after the context-router replay.\n"
    "- Actions Performed: compared the block transcript from 2026-09-24T00-33-31-978Z_01a0d0d4-f349-73d3-a379-98e720020fa5.jsonl with summaries.digest in ctx-engine.db; the row was absent, while requests.raw contained only a 4000-character excerpt.\n"
    "- Conclusion: block 7f3a91c2e0b44d18 was not yet condensed; the debugger must show the full bounded session block and must not treat the request excerpt as its summary."
)


class CondenserError(RuntimeError):
    pass


# ----------------------------- request shaping -----------------------------

# The global note length, resolved at request time (like BUDGET) so `/config` can move it without a restart;
# a restart falls back to PI_CONTEXT_ROUTER_SUMMARY_TOKENS. Tests and callers may override either entry.
SUMMARY = {"tokens": DEFAULT_SUMMARY_TOKENS, "set_at": 0.0}
# Reserve for the framed summary items when fitting how many fit: the old 64-char slack expressed in tokens.
SUMMARY_ROOM_HEADROOM_TOKENS = round(64 / CHARS_PER_TOKEN)
# Character step of the summary-room clip loop; a text-shaping decrement, not a budget unit.
SUMMARY_TRIM_CHARS = 64


def summary_target(tokens: int | None = None) -> int:
    """The note's global token ceiling, snapped onto SUMMARY_TOKEN_STEPS; part of the summary cache key."""
    return snap_summary_tokens(SUMMARY["tokens"] if tokens is None else tokens)


def summary_hint(transcript: str, target: int | None = None, tokens: int | None = None) -> str:
    """Length suffix appended to the bottom of the prompt so the global token ceiling is the last thing read.

    Exact token counting is unavailable here, so the block's own size falls back to CHARS_PER_TOKEN (3.5).
    """
    tokens = summary_target(tokens) if target is None else int(target)
    block_tokens = estimate_tokens(transcript)
    return f"Length hint: this block is about {block_tokens} tokens; keep the whole note within {tokens} tokens."


def compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


IMAGE_PART_TYPES = {"image", "input_image", "output_image"}


def is_image_part(part: object) -> bool:
    return isinstance(part, dict) and (part.get("type") in IMAGE_PART_TYPES or "image_url" in part)


def strip_images(value: object) -> object:
    """Copy with image parts removed: image payloads are forwarded untouched and stripped when a block is compacted."""
    if isinstance(value, list):
        return [strip_images(item) for item in value if not is_image_part(item)]
    if isinstance(value, dict):
        return {key: strip_images(item) for key, item in value.items()}
    return value


def estimate_chars(body: dict) -> int:
    """Chat Completions (`messages`) and Responses (`instructions`/`input`) share one text-only estimate.

    Images are deliberately not measured: a base64 screenshot must not count as context and trigger
    compaction or overflow. The text around the image still counts, and a compacted block loses the image.
    This is the char primitive; budget decisions use `estimate_tokens`.
    """
    return sum(len(compact_json(strip_images(body.get(key)))) for key in ("instructions", "tools", "input", "messages") if key in body)


def estimate_tokens(value: object) -> int:
    """Token count for a request body or a raw text/bytes payload, the router's single budget unit.

    No tokenizer runs in this process, so text is measured at the CHARS_PER_TOKEN fallback (3.5 chars =
    1 token). A body keeps the image exclusion: a base64 payload never inflates the estimate.
    """
    chars = estimate_chars(value) if isinstance(value, dict) else len(value or b"")
    return round(chars / CHARS_PER_TOKEN)


# ----------------------------- cost accounting -----------------------------

TAIL_KEEP = 65536
RATES: dict = {}


def model_rates(model: str) -> dict | None:
    """Declared per-million rates for a model id, read once from models-store.json; None when unknown."""
    if not RATES:
        try:
            with open(os.path.join(os.path.dirname(AUTH_PATH), "models-store.json")) as handle:
                for value in json.load(handle).values():
                    for item in value.get("models") or []:
                        if item.get("id") and item.get("cost"):
                            RATES.setdefault(str(item["id"]), item["cost"])
        except (OSError, ValueError):
            return None
    if not model or not RATES:
        return None
    if model in RATES:
        return RATES[model]
    for ident, rates in RATES.items():
        if model.endswith(ident) or ident.endswith(model):
            return rates
    return None


def cost_of(fresh: int, cached: int, out: int, rates: dict) -> float:
    """USD for one call from fresh/cached prompt tokens and output tokens at per-million rates."""
    return (fresh * rates.get("input", 0) + cached * rates.get("cacheRead", 0) + out * rates.get("output", 0)) / 1e6


def cache_split(total: int, fresh: int, cached: int) -> tuple[int, int]:
    """Split `total` into (fresh, cached) at the cache-hit fraction `cached / (fresh + cached)` observed on the call."""
    seen = fresh + cached
    if not seen:
        return total, 0
    held = int(round(total * cached / seen))
    return total - held, held


def billed_tokens(usage: dict) -> tuple[int, int, int]:
    """(fresh prompt, cached prompt, output) tokens from a Chat Completions or Responses usage object."""
    if not usage:
        return 0, 0, 0
    if "prompt_tokens" in usage:
        cached = int(usage.get("prompt_cache_hit_tokens") or 0)
        return max(0, int(usage.get("prompt_tokens") or 0) - cached), cached, int(usage.get("completion_tokens") or 0)
    cached = int((usage.get("input_tokens_details") or {}).get("cached_tokens") or 0)
    return max(0, int(usage.get("input_tokens") or 0) - cached), cached, int(usage.get("output_tokens") or 0)


def usage_from_tail(tail: bytes) -> dict:
    """Last `usage` object in a proxied response tail; {} when the provider sent none."""
    text = tail.decode("utf-8", "replace")
    start = text.rfind('"usage"')
    start = text.find("{", start) if start != -1 else -1
    if start == -1:
        return {}
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:index + 1])
                except json.JSONDecodeError:
                    return {}
    return {}


def build_cost(model: str, raw_tokens: int, usage: dict, condenser: dict | None) -> dict | None:
    """Real upstream billing for one request plus the net saving after the condenser's own cost. The
    no-compaction baseline prices the raw token count at the cache-hit fraction the call itself observed —
    the provider would have served most of that prompt from cache, and pricing it all as fresh input would
    overstate the saving by the cache discount (DeepSeek: 0.006 vs 0.30 USD/M)."""
    fresh, cached, out = billed_tokens(usage)
    cond = condenser or {}
    calls = int(cond.get("calls") or 0)
    if not (fresh or cached or out or calls):
        return None
    entry = {"model": model, "in": fresh, "cached": cached, "out": out, "condenser_calls": calls,
             "condenser_in": int(cond.get("in") or 0), "condenser_out": int(cond.get("out") or 0)}
    rates = model_rates(model)
    if not rates:
        return entry
    sent = cost_of(fresh, cached, out, rates)
    raw_fresh, raw_cached = cache_split(int(raw_tokens or 0), fresh, cached)
    raw = cost_of(raw_fresh, raw_cached, out, rates)
    condenser_usd = float(cond.get("usd") or 0.0)
    net = raw - sent - condenser_usd
    entry.update(raw_cached=raw_cached, sent_usd=round(sent, 6), raw_usd=round(raw, 6), condenser_usd=round(condenser_usd, 6),
                 net_usd=round(net, 6), net_percent=round(100 * net / raw, 1) if raw else 0.0)
    return entry


def has_image_content(items: list) -> bool:
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("content"), list):
            continue
        if any(is_image_part(part) for part in item["content"]):
            return True
    return False


def normalized_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


BULLET_SPLIT = re.compile(r"(?:^|\s)(?:[-\u2022]\s*|(?=(?:Task|Work done|Results)\s*:))")

# The condenser's three labelled sections, in canonical order. A summary carrying all three is a cache hit;
# anything else (old bullets, prose) is re-condensed and overwritten in place by `cache_put`.
SECTION_LABELS = ("Task/Query", "Actions Performed", "Conclusion")
_SECTION_RE = re.compile(r"(?P<label>Task\s*/\s*Query|Actions\s+Performed|Conclusion)\s*:\s*", re.I)
_CANON_LABEL = {"task/query": "Task/Query", "actions performed": "Actions Performed", "conclusion": "Conclusion"}


def normalize_summary(text: str) -> str:
    """Enforce the three labelled sections in code: the local 2B model answers as one paragraph and ignores the prompt."""
    clean = re.sub(r"\s+", " ", re.sub(r"[*`#]+", "", strip_thinking(str(text or "")))).strip()
    matches = list(_SECTION_RE.finditer(clean))
    if matches:
        found: dict[str, str] = {}
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(clean)
            key = re.sub(r"\s+", " ", match.group("label")).lower()
            body = clean[match.end():end].strip(" -•")
            if key in _CANON_LABEL and body:
                found[_CANON_LABEL[key]] = body
        if found:
            return "\n".join(f"- {label}: {found[label]}" for label in SECTION_LABELS if label in found)
    # No labelled sections: fall back to one '- ' bullet per chunk, never lose the text.
    parts = (re.sub(r"^\d+[.)]\s*", "", part).strip(" -•") for part in BULLET_SPLIT.split(clean))
    return "\n".join(f"- {part}" for part in parts if part)


def is_current_summary(text: str) -> bool:
    """True when text is already in the labelled shape, so a stored row is a real cache hit, not stale prose."""
    lines = [line for line in (text or "").splitlines() if line.strip()]
    if not lines or not all(line.startswith("- ") for line in lines):
        return False
    return all(any(label + ":" in line for line in lines) for label in SECTION_LABELS)


def content_text(content: object) -> str:
    """Text of an OpenAI content value (string or parts list); non-text parts and metadata are ignored."""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    if isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part["text"])
    return " ".join(parts)


def item_description(item: object) -> str:
    if not isinstance(item, dict):
        return normalized_text(item)
    role = item.get("role")
    content = item.get("content")
    if role in {"tool", "toolResult", "tool_result"}:
        return f"tool output: {normalized_text(content_text(content))}"
    if role == "assistant" and isinstance(item.get("tool_calls"), list):
        calls = ", ".join(f"{normalized_text((c.get('function') or {}).get('name'))}({normalized_text((c.get('function') or {}).get('arguments'))})" for c in item["tool_calls"] if isinstance(c, dict))
        prose = normalized_text(content_text(content))
        return f"assistant called {calls}; said: {prose}" if calls else f"assistant: {prose}"
    if role in {"user", "assistant", "developer", "system"}:
        text = normalized_text(content_text(content))
        if not text and has_image_content([item]):
            text = "[image]"  # compaction strips the image; keep the fact that one was there
        return f"{role}: {text}"
    kind = item.get("type", "item")
    if kind in {"function_call", "custom_tool_call"}:
        return f"assistant called {normalized_text(item.get('name'))}: {normalized_text(item.get('arguments', item.get('input')))}"
    if kind in {"function_call_output", "custom_tool_call_output"}:
        return f"tool output: {normalized_text(item.get('output'))}"
    if kind == "reasoning":
        summary = item.get("summary")
        return f"assistant reasoning: {normalized_text(summary) if summary else '[encrypted reasoning]'}"
    # Anything else is a harness event, not core OpenAI schema: keep its text, drop the envelope, never dump JSON.
    text = normalized_text(content_text(content) or item.get("output") or "")
    return f"{role or kind}: {text}" if text else ""


def clip_middle(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    if limit < 16:
        return text[: limit - 1] + "…"
    head = max(1, int((limit - 3) * 0.65))
    tail = max(1, limit - 3 - head)
    return f"{text[:head]} … {text[-tail:]}"


def text_paths(value: object, path: tuple = ()):
    """(path, length) of every string under value, image payloads excluded: the mechanical trimmer's map."""
    if isinstance(value, str):
        yield path, len(value)
    elif isinstance(value, dict):
        if not is_image_part(value):
            for key, item in value.items():
                yield from text_paths(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if not is_image_part(item):
                yield from text_paths(item, path + (index,))


def path_value(value: object, path: tuple) -> object:
    for key in path:
        value = value[key]
    return value


def path_assign(value: object, path: tuple, text: str) -> None:
    *head, last = path
    for key in head:
        value = value[key]
    value[last] = text


# ----------------------------- settled-turn blocks -----------------------------

def split_blocks(items: list) -> list[list]:
    """One block per user message: [user ... assistant/tool items ...] up to the next user message."""
    blocks: list[list] = []
    for item in items:
        starts = isinstance(item, dict) and item.get("role") == "user"
        if starts or not blocks:
            blocks.append([item])
        else:
            blocks[-1].append(item)
    return blocks


def split_message_blocks(messages: list) -> tuple[list, list]:
    """Chat Completions: leading system messages are instructions, then one block per user message."""
    prefix: list = []
    index = 0
    while index < len(messages) and isinstance(messages[index], dict) and messages[index].get("role") == "system":
        prefix.append(messages[index])
        index += 1
    blocks: list[list] = []
    for item in messages[index:]:
        if (isinstance(item, dict) and item.get("role") == "user") or not blocks:
            blocks.append([item])
        else:
            blocks[-1].append(item)
    return prefix, blocks


def block_id(block: list) -> str:
    return hashlib.sha256(compact_json(block).encode()).hexdigest()[:16]


def block_transcript(block: list) -> str:
    return "\n".join(filter(None, (item_description(item) for item in block)))


# What one compaction did to one request, small enough to keep per row: per completed block, its size and either
# the summary that replaced it (with an excerpt) or that it was dropped. Full bodies are far too large to retain.
PLAN_LIMIT = 20000
PLAN_SUMMARY_CHARS = 600
PLAN_PREVIEW_CHARS = 300


def compaction_plan(completed: list[list], summaries: list[tuple[str, str]]) -> str:
    """Per-block record of one compaction, so the dashboard can diff what each removed turn became."""
    texts = dict(summaries)
    entries = []
    for block in completed:
        ident = block_id(block)
        summary = texts.get(ident)
        entry = {"id": ident, "tokens": estimate_tokens(compact_json(block)), "chars": len(compact_json(block)), "dropped": summary is None}
        if summary is not None and summary != PENDING_SUMMARY:
            entry["summary"] = clip_middle(summary, PLAN_SUMMARY_CHARS)
            entry["summary_tokens"] = estimate_tokens(summary)
            entry["summary_chars"] = len(summary)
            entry["preview"] = clip_middle(block_transcript(block), PLAN_PREVIEW_CHARS)
        elif summary is not None:
            entry["dropped"] = True
            entry["pending"] = True
        entries.append(entry)
    plan = {"kept": len(summaries), "blocks": entries}
    while entries and len(compact_json(plan)) > PLAN_LIMIT:
        entries.pop(0)
        plan["omitted"] = plan.get("omitted", 0) + 1
    return compact_json(plan)


def http_json(url: str, payload: dict, headers: dict[str, str], timeout: float) -> dict:
    parts = urlsplit(url)
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    kwargs = {"timeout": timeout}
    if parts.scheme == "https":
        kwargs["context"] = ssl.create_default_context()
    conn = cls(parts.hostname, parts.port, **kwargs)
    try:
        conn.request("POST", parts.path or "/", body=compact_json(payload).encode(), headers=headers)
        response = conn.getresponse()
        body = response.read()
        if response.status >= 400:
            raise CondenserError(f"condenser HTTP {response.status}: {body[:200].decode('utf-8', 'replace')!r}")
        return json.loads(body)
    finally:
        conn.close()


def strip_thinking(text: str) -> str:
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", " ", text or "", flags=re.S | re.I).strip()


def fit_summaries(summaries: list, room: int, make_item, bump_dropped) -> list:
    """Keep as many summaries as fit `room` tokens, dropping the OLDEST first; never touch the live turn."""
    while summaries and sum(estimate_tokens(compact_json(make_item(i, t))) for i, t in summaries) > room:
        summaries.pop(0)
        bump_dropped()
    while summaries and sum(estimate_tokens(compact_json(make_item(i, t))) for i, t in summaries) > room:
        ident, text = summaries.pop()
        summaries.append((ident, clip_middle(text, max(0, len(text) - SUMMARY_TRIM_CHARS))))
    return summaries

