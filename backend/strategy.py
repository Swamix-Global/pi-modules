"""Context strategy: turn anatomy, the pinned L1 drop frontier, and whole-turn trimming.

Fast Mode (L1) drops tool calls and tool results from before the current turn; Standard
Mode (L2) then folds the surviving history through the condenser (see `condenser`).
A turn runs from a user message to its last successful assistant response, and tool
blocks always travel whole — a result without its call is rejected by every provider.

The one invariant everything below serves: a request must start with the same bytes as
the request before it. Cutting a scattered set of blocks out of the middle of history
shifts the prompt head, so the host can no longer serve the tail from cache and re-bills
it as fresh input. Only contiguous front cuts and appended turns are allowed.
"""

from __future__ import annotations

import hashlib
import json

from .config import BUDGET, CHARS_PER_TOKEN

MEDIA_TYPES = frozenset({"image", "image_url", "input_image", "file", "audio"})
THINKING_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning"})  # kept verbatim
TOOL_RESULT_ROLES = frozenset({"toolResult", "tool"})


# --- sizing -------------------------------------------------------------------------------
def token_estimate(text: str) -> int:
    """Chars-to-tokens estimate; only ever used to pick drop victims."""
    return max(1, int(len(text) / CHARS_PER_TOKEN)) if text else 0


def message_tokens(message: dict) -> int:
    """Estimated tokens, ignoring encrypted thinking/reasoning blobs."""
    content = message.get("content")
    body = token_estimate(content) if isinstance(content, str) else sum(token_estimate(json.dumps(block, ensure_ascii=False)) for block in content or () if isinstance(block, dict) and block.get("type") not in THINKING_TYPES)
    return body + sum(token_estimate(json.dumps(call, ensure_ascii=False)) for call in message.get("tool_calls") or ())


def overhead_tokens(messages: list[dict]) -> int:
    """Estimated tokens across the whole conversation."""
    return sum(map(message_tokens, messages))


# --- message anatomy ----------------------------------------------------------------------
def _call_id(message: dict) -> str | None:
    """Tool-call id of a tool result, in either wire format."""
    return message.get("toolCallId") or message.get("tool_call_id")


def _call_ids(message: dict) -> set[str]:
    """Every tool-call id a message announces, in either wire format."""
    announced = {block.get("id") for block in message.get("content") or () if isinstance(block, dict) and block.get("type") == "toolCall"}
    return (announced | {call.get("id") for call in message.get("tool_calls") or ()}) - {None}


def _has_tool_calls(message: dict) -> bool:
    """True for Pi blocks (toolCall) and OpenAI wire format (tool_calls)."""
    return bool(message.get("tool_calls")) or any(isinstance(block, dict) and block.get("type") == "toolCall" for block in message.get("content") or ())


def _is_media(block: object) -> bool:
    """True for any inline media block that balloons or breaks context."""
    return isinstance(block, dict) and block.get("type") in MEDIA_TYPES


def _is_tool_result(message: dict) -> bool:
    return message.get("role") in TOOL_RESULT_ROLES


def content_sig(message: dict) -> str:
    """Content hash that identifies an already-condensed entry across requests."""
    return hashlib.sha1(json.dumps(message.get("content"), sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# --- blocks and turns ---------------------------------------------------------------------
def count_tool_blocks(messages: list[dict]) -> int:
    """Droppable blocks: assistant call turns plus tool results, either wire format."""
    return sum(1 for m in messages if _is_tool_result(m) or (m.get("role") == "assistant" and _has_tool_calls(m)))


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


# --- Fast Mode | L1 -----------------------------------------------------------------------
def tool_pairs(messages: list[dict]) -> list[list[int]]:
    """Index groups of each pre-turn tool-call pair, oldest first, results included."""
    turn = _turn_start(messages)
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


def drop_oldest_tool_pairs(messages: list[dict], budget: int) -> tuple[list[dict], int]:
    """N-1 policy: shed whole tool pairs, oldest first, until the payload fits the budget.

    Tool output is what balloons a transcript; prose is the evidence the next turn argues
    from. So L1 drops a call turn together with every result it owns, never a plain
    user/assistant message, and stops the moment the payload is under `budget` — no low
    watermark, no pinned frontier. Pairs go oldest-first (N-1 of them before the newest):
    the newest pair is dropped last, and only when the payload is still over budget without
    it, so a single oversized dump cannot ride through untouched.
    """
    total = overhead_tokens(messages)
    if total <= budget:
        return messages, 0
    drop: set[int] = set()
    for group in tool_pairs(messages):
        if total <= budget:
            break
        drop.update(group)
        total -= sum(message_tokens(messages[i]) for i in group)
    if not drop:
        return messages, 0  # prose only: nothing is droppable
    return [message for i, message in enumerate(messages) if i not in drop], len(drop)


def drop_blocks_before_current_turn(messages: list[dict], budget: int = BUDGET) -> tuple[list, int]:
    """L1: strip media, then shed the oldest whole tool pairs (N-1 policy).

    Deliberately ignores the client's usage snapshot: it counts the history *before* this
    router trimmed it, so it stays above the budget for the rest of the session and would
    re-trim on every single request.
    """
    stripped, _ = strip_media(messages)
    return drop_oldest_tool_pairs(stripped, budget)


# --- Standard Mode | L2 -------------------------------------------------------------------
def condensed_prefix_build(messages: list[dict], prev_entry_sigs: list[str]) -> tuple[list, list]:
    """Entries the condenser may fold: pre-turn, non-system, and never the same twice."""
    last_user = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=-1)
    entries = [m for m in messages[:last_user] if m.get("role") != "system"] if last_user > 0 else []
    if not entries and len(messages) >= 2 and (messages[-2].get("role") == "assistant" and _has_tool_calls(messages[-2]) and _is_tool_result(messages[-1])):
        entries = messages[-2:]  # single-turn session: one giant tool dump still condenses
    sigs = [*map(content_sig, entries)]
    return ([], prev_entry_sigs) if entries and sigs == prev_entry_sigs else (entries, sigs)


def context_messages(messages: list[dict], budget: int) -> list[dict]:
    """Trim to the newest whole turns within budget, never rewriting a single byte."""
    split = next((i for i, m in enumerate(messages) if m.get("role") != "system"), len(messages))
    head, rest = messages[:split], messages[split:]
    turns: list[list[dict]] = []
    for message in rest:
        if message.get("role") == "user" or not turns:
            turns.append([message])
        else:
            turns[-1].append(message)
    sizes = [*map(overhead_tokens, turns)]
    start, total = len(turns), 0
    while start and (start == len(turns) or total + sizes[start - 1] <= budget):
        start, total = start - 1, total + sizes[start - 1]
    return head + [message for turn in turns[start:] for message in turn]
