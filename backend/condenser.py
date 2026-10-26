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

from .config import (CACHE_DIR, CHARS_PER_TOKEN, CONDENSE_RETRIES, SG_LANG_BASE,
                     SG_LANG_MODEL)

CONDENSE_PROMPT = (
    "You compress a working-memory dump from a coding agent session. "
    "Preserve: file paths, function and variable names, commands, errors, "
    "decisions, open questions, task state. Drop: tool noise, retries, "
    "superseded attempts. Output only the compressed notes, no preamble."
)


def served_model() -> str:
    """Served model id of the local host, or 'default' when it is down."""
    try:
        return requests.get(f"{SG_LANG_BASE}/models", timeout=(3, 10)).json()["data"][0]["id"]
    except Exception:
        return "default"


def local_ai(system_prompt: str, user_message: str, max_tokens: int = 2048) -> str:
    """Ask the local granite model; durable file cache keyed by the input hash."""
    key = hashlib.sha1(json.dumps([system_prompt, user_message, max_tokens],
                                  ensure_ascii=False).encode()).hexdigest()
    path = CACHE_DIR / f"{key}.txt"
    if path.exists():
        return path.read_text()
    payload = {"model": SG_LANG_MODEL or served_model(), "max_tokens": max_tokens,
               "temperature": 0.2, "chat_template_kwargs": {"enable_thinking": False},
               "messages": [{"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_message}]}
    last: Exception | None = None
    for _ in range(CONDENSE_RETRIES + 1):
        try:
            resp = requests.post(f"{SG_LANG_BASE}/chat/completions", json=payload,
                                 timeout=(5, 120))
            resp.raise_for_status()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text := resp.json()["choices"][0]["message"]["content"].strip())
            return text
        except (requests.RequestException, KeyError, IndexError, ValueError) as exc:
            last = exc
    raise RuntimeError(f"local_ai failed after {CONDENSE_RETRIES + 1} attempts: {last}")


# --- entry rendering ----------------------------------------------------------------------
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
        args = json.dumps(block.get("arguments", {}), ensure_ascii=False)
        return f"[{role} toolCall {block.get('name', '?')}] {args}"
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
    """Fold entries into notes through `local_ai`; returns (notes, elapsed ms)."""
    chars = int(target_tokens * CHARS_PER_TOKEN)
    started = time.perf_counter()
    text = local_ai(f"{CONDENSE_PROMPT} Keep it under ~{chars} characters.",
                    "\n\n".join(filter(None, map(_format_entry, entries))),
                    max_tokens=max(256, min(4096, int(chars / 3.2))))
    return text, (time.perf_counter() - started) * 1000
