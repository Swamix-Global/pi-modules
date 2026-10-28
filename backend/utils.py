"""Context-router utilities: wire handling and N-1 tool stripping.

All environment configuration belongs in config.py; HTTP orchestration belongs in
app.py. Keep this backend to exactly those three modules and nothing else.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import requests

from .config import (
    AUTH_PATH,
    GZIP_MIN_BYTES,
    GZIP_PROVIDERS,
    HOP_HEADERS,
    MANAGED_AUTH,
    SG_LANG_BASE,
)

# --- Wire metadata, authentication, headers and compression ---

def parse_snapshot(text: str) -> dict[str, Any]:
    """Split a raw body from the `_pi_ctx` usage snapshot Pi appends to it.

    Reported usage is telemetry only. The marker must follow the complete JSON body,
    never match inside user/tool text. Booleans are not token counts.
    """
    source = text.lstrip()
    body, end = json.JSONDecoder().raw_decode(source)
    trailer = source[end:].strip()
    if not trailer.startswith("_pi_ctx::"):
        raise ValueError("no _pi_ctx snapshot trailer")
    data = json.loads(trailer[len("_pi_ctx::"):])
    if not isinstance(data, dict):
        raise ValueError("snapshot must be an object")
    usage = data.get("usage", data.get("tokens"))
    if type(usage) is not int or usage < 0:
        raise ValueError("snapshot has no nonnegative integer usage")
    return {"usage": usage, "body": body}


def auth_map() -> dict[str, str]:
    """provider -> bearer key from auth.json; a missing file or entry is fine."""
    try:
        raw = json.loads(AUTH_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return {provider: key for provider, entry in raw.items() if isinstance(entry, dict)
            and isinstance(key := entry.get("key") or entry.get("apiKey") or entry.get("access"),
                           str) and key}


def upstream_headers(provider: str, headers, auth: dict[str, str]) -> dict[str, str]:
    """Strip hop headers; managed providers get router-injected auth, not client auth."""
    out = {key: value for key, value in headers.items() if key.lower() not in HOP_HEADERS}
    lowered = {key.lower() for key in out}
    if "content-type" not in lowered:
        out["Content-Type"] = "application/json"
    if "accept-encoding" not in lowered:
        out["Accept-Encoding"] = "gzip, deflate"
    if provider not in auth:
        return out
    return {**{key: value for key, value in out.items() if key.lower() not in MANAGED_AUTH},
            "Authorization": f"Bearer {auth[provider]}"}


def maybe_gzip(provider: str, body: bytes, headers: dict[str, str]) -> bytes:
    """Gzip big cloud-bound bodies; edges decode them, local hosts cannot."""
    if provider in GZIP_PROVIDERS and len(body) >= GZIP_MIN_BYTES:
        headers["Content-Encoding"], body = "gzip", gzip.compress(body)
    return body


# --- Message sizing, turn anatomy and complete tool pairs ---

MEDIA_TYPES = frozenset({"image", "image_url", "input_image", "file", "audio"})


def _call_id(message: dict) -> str | None:
    """Tool-call id of a tool result, in either wire format."""
    return message.get("toolCallId") or message.get("tool_call_id")


def _call_ids(message: dict) -> set[str]:
    """Every tool-call id a message announces, in either wire format."""
    announced = {block.get("id") for block in message.get("content") or () if isinstance(block, dict) and block.get("type") == "toolCall"}
    return (announced | {call.get("id") for call in message.get("tool_calls") or ()}) - {None}


def _is_media(block: object) -> bool:
    """True for any inline media block that balloons or breaks context."""
    return isinstance(block, dict) and block.get("type") in MEDIA_TYPES


def _turn_start(messages: list[dict]) -> int:
    """Index of the current turn: everything from here on stays."""
    return max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=len(messages))


def strip_media(messages: list[dict]) -> tuple[list[dict], int]:
    """Drop media blocks before the current turn; screenshots balloon, providers reject."""
    current = _turn_start(messages)
    out: list[dict] = []
    removed = 0
    for i, message in enumerate(messages):
        content = message.get("content")
        if i >= current or not isinstance(content, list):
            out.append(message)
            continue
        kept = [block for block in content if not _is_media(block)]
        removed += len(content) - len(kept)
        out.append(message if len(kept) == len(content) else {**message, "content": kept})
    return out, removed


def repair_tool_pairs(messages: list[dict]) -> list[dict]:
    """Drop results whose call was cut and calls whose results all went with it.

    Any cut that is not block-aligned leaves an orphan `tool` message, and providers reject
    the whole request (`messages with role 'tool' must be a response to a preceding message
    with 'tool_calls'`). The input list is returned as-is when nothing needs repair, so an
    untouched request keeps its exact bytes.
    """
    out: list[dict] = []
    announced: set[str] = set()
    changed = False
    for message in messages:
        if ids := _call_ids(message):
            announced = set(ids)
            out.append(message)
        elif (call_id := _call_id(message)) is not None:
            if call_id in announced:
                out.append(message)
            else:
                changed = True
        else:
            out.append(message)
    answered = {_call_id(message) for message in out}
    orphaned = [message for message in out if (ids := _call_ids(message)) and not ids & answered]
    if orphaned:
        dropped = {id(message) for message in orphaned}
        out = [message for message in out if id(message) not in dropped]
        changed = True
    return out if changed else messages


def tool_pairs(messages: list[dict], include_current: bool = False) -> list[list[int]]:
    """Index groups of each pre-turn tool-call pair, oldest first, results included."""
    turn = len(messages) if include_current else _turn_start(messages)
    groups: list[list[int]] = []
    owner: dict[str, int] = {}
    for i, message in enumerate(messages[:turn]):
        ids = _call_ids(message)
        if ids:
            groups.append([i])
            for call_id in ids:
                owner[call_id] = len(groups) - 1
        elif (call_id := _call_id(message)) is not None and call_id in owner:
            groups[owner[call_id]].append(i)
    return groups


def strip_to_newest_pair(messages: list[dict]) -> tuple[list[dict], int]:
    """N-1 tool stripping: drop every complete tool pair except the newest one.

    Stateless and deterministic — the same transcript always yields the same bytes,
    so consecutive requests share a byte-identical prompt prefix. Prose is never
    cut, incomplete (unanswered) pairs are never cut, and the newest complete pair
    always survives. Returns the (possibly unchanged) message list and the number
    of pairs dropped.
    """
    complete = []
    for group in tool_pairs(messages, include_current=True):
        ids = _call_ids(messages[group[0]])
        answered = {_call_id(messages[i]) for i in group[1:]}
        if ids and ids <= answered:
            complete.append(group)
    if len(complete) <= 1:
        return messages, 0
    victims = {index for group in complete[:-1] for index in group}
    return [m for i, m in enumerate(messages) if i not in victims], len(complete) - 1


# --- Local model host (liveness ping) ---


def served_model() -> str:
    """Served model id of the local host, or 'default' when it is down."""
    try:
        return requests.get(f"{SG_LANG_BASE}/models", timeout=(3, 10)).json()["data"][0]["id"]
    except Exception:
        return "default"
