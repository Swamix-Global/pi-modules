"""Standard Mode (L2): fold old history into notes through the local SGLang condenser.

The condenser is advisory: every call is bounded by retries and a durable file cache, and
the caller ships the turn untouched when it raises, because a missing summary must never
cost the user their request.
"""

from __future__ import annotations

import hashlib
import json
import time

import requests

from .config import (CACHE_DIR, CONDENSE_RETRIES, SG_LANG_BASE, SG_LANG_MODEL,
                     WORDS_PER_TOKEN)

CONDENSE_PROMPT = (
    "You compress a coding-agent dump into notes for an agent that must continue the task.\n"
    "Reply with exactly this template, one entry per line, and omit a label only when nothing real fits it:\n"
    "TASK: one line stating the goal\n"
    "STATE: what is done, running or blocked now — newest status only\n"
    "FACTS: paths, symbols, commands, versions, numbers, file:line — copied verbatim\n"
    "DECISIONS: 'choice because reason'\n"
    "ERRORS: 'symptom -> cause -> fix -> status'\n"
    "OPEN: unknowns and failed checks\n"
    "NEXT: the single next action\n"
    "Rules: copy identifiers and numbers exactly and never invent one — write unknown instead; merge "
    "duplicates, retries and superseded attempts, keeping only the latest status; no logs, no code "
    "fences, no preamble; under 200 words.\n"
    "Example reply:\n"
    "TASK: stop per-turn cache re-billing in the context router\n"
    "STATE: budget 60k with cut fraction 0.4 is live; 2 trims in 11 requests, 0 frontier resets\n"
    "FACTS: cache-stats.js:41 NOISE_FLOOR_TOKENS=1024; utils.py passes() cuts to budget*0.4\n"
    "DECISIONS: added a hysteresis watermark because an exact-budget cut moved the frontier every request\n"
    "ERRORS: router 503 at 18:41 -> transient -> curl retry ok\n"
    "OPEN: whether 0 resets still holds after a compaction turn\n"
    "NEXT: replay one compaction turn and compare cacheRead with the previous run"
)


def served_model() -> str:
    """Served model id of the local host, or 'default' when it is down."""
    try:
        return requests.get(f"{SG_LANG_BASE}/models", timeout=(3, 10)).json()["data"][0]["id"]
    except Exception:
        return "default"


def local_ai(system_prompt: str, user_message: str, max_tokens: int = 2048) -> str:
    """Ask the local granite model; durable file cache keyed by the input hash."""
    key = hashlib.sha1(json.dumps([system_prompt, user_message, max_tokens], ensure_ascii=False).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.txt"
    if path.exists():
        return path.read_text()
    payload = {
        "model": SG_LANG_MODEL or served_model(),
        "max_tokens": max_tokens,
        "temperature": 0.2,
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_message}],
    }
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


# --- entry rendering ----------------------------------------------------------------------
def _format_entry(message: dict) -> str:
    """Render one wire message — string content, Pi blocks, or OpenAI tool_calls — as lines."""
    role = message.get("role", "?")
    parts = [f"[{role} toolCall {(fn := call.get('function') or {}).get('name', '?')}] {fn.get('arguments', '')}"
             for call in message.get("tool_calls") or ()]
    content = message.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else [b for b in content or () if isinstance(b, dict)]
    for block in blocks:
        kind, text = block.get("type", "?"), block.get("text", "")
        if kind == "text":
            parts.append(f"[{role}] {text}")
        elif kind == "toolCall":
            parts.append(f"[{role} toolCall {block.get('name', '?')}] {json.dumps(block.get('arguments', {}), ensure_ascii=False)}")
        elif kind == "image":
            parts.append(f"[{role} image omitted]")
        else:
            parts.append(f"[{role} {kind}] {json.dumps(block, ensure_ascii=False)[:400]}")
    return "\n".join(filter(None, parts))


def word_budget(target_tokens: int) -> int:
    """Words the notes may spend for a token target (1 token ≈ ¾ word in English)."""
    return max(64, int(target_tokens * WORDS_PER_TOKEN))


def count_words(text: str) -> int:
    """Words in a note, the unit every condenser budget is expressed in."""
    return len(text.split())


def fit_words(text: str, limit: int) -> str:
    """Enforce the word budget in code; the model's own length is never trusted."""
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join([*words[:limit - 1], words[limit - 1] + "…"])


def condense(entries: list[dict], target_tokens: int) -> tuple[str, float]:
    """Fold entries into notes through `local_ai`; returns (notes, elapsed ms)."""
    words = word_budget(target_tokens)
    started = time.perf_counter()
    raw = local_ai(f"{CONDENSE_PROMPT} Keep it under ~{words} words.",
                   "\n\n".join(filter(None, map(_format_entry, entries))),
                   max_tokens=max(256, min(4096, words * 4 // 3)))
    return fit_words(raw, words), (time.perf_counter() - started) * 1000
