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
from collections import OrderedDict

from .config import BUDGET, CHARS_PER_TOKEN, CUT_FRACTION, FRONTIER_CAP

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


def message_sig(message: dict) -> str:
    """Stable identity of one message, used to validate a pinned cut boundary."""
    body = {"role": message.get("role"), "id": _call_id(message), "content": message.get("content"), "calls": [call.get("id") for call in message.get("tool_calls") or ()]}
    return hashlib.sha1(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# --- blocks and turns ---------------------------------------------------------------------
def _blocks(messages: list[dict]) -> list[list[dict]]:
    """Segment into atomic blocks: a call turn absorbs exactly the results it owns."""
    blocks: list[list[dict]] = []
    for i, message in enumerate(messages):
        owner = blocks[-1] if blocks else None
        if i and owner and owner[-1] is messages[i - 1] and _is_tool_result(message) and _call_id(message) in _call_ids(owner[0]):
            owner.append(message)
        else:
            blocks.append([message])
    return blocks


def count_tool_blocks(messages: list[dict]) -> int:
    """Droppable blocks: assistant call turns plus tool results, either wire format."""
    return sum(1 for m in messages if _is_tool_result(m) or (m.get("role") == "assistant" and _has_tool_calls(m)))


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
def cut_prefix(messages: list[dict], budget: int, low: int, anchor: int | None = None, start: int | None = None) -> tuple[list[dict], int]:
    """Drop a contiguous, block-aligned prefix of old turns until the payload fits `low`.

    `anchor` is the protected head (system messages plus the first user turn); `start` is the
    current drop frontier, which defaults to the anchor. `low` is measured on what would be
    forwarded — the head plus everything from `start` on — so an already-dropped prefix is
    never counted twice. `low` sits below `budget` on purpose: cutting exactly to the budget
    leaves no headroom, so the next turn re-crosses it and moves the frontier again, paying a
    cache rebuild per request instead of per budget band.
    """
    anchor = _anchor(messages) if anchor is None else anchor
    start = max(anchor, start if start is not None else anchor)
    head, view = messages[:anchor], messages[start:]
    if overhead_tokens(head) + overhead_tokens(view) <= budget:
        return messages, 0
    turn = _turn_start(messages)
    over = overhead_tokens(head) + overhead_tokens(view) - low
    dropped = 0
    for block in _blocks(messages[start:turn]):
        if over <= 0:
            break
        over -= sum(map(message_tokens, block))
        dropped += len(block)
    if not dropped:
        return messages, 0  # nothing cuttable before the current turn: keep everything
    return messages[:anchor] + messages[start + dropped :], dropped


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
        self.resets = 0  # times a pin could not be relocated and history was re-cut

    @property
    def size(self) -> int:
        """Conversations currently pinned."""
        return len(self._pins)

    @staticmethod
    def key(messages: list[dict]) -> str:
        """Conversation identity: the system prompt plus the first turn of the transcript."""
        seed = next((m for m in messages if m.get("role") != "system"), {})
        return hashlib.sha1((json.dumps(messages[0] if messages else {}, sort_keys=True) + "|" + content_sig(seed)).encode()).hexdigest()[:16]

    def _remember(self, key: str, pin: tuple[int, str]) -> None:
        """Store the pin, evicting the least recently used conversation."""
        self._pins[key] = pin
        self._pins.move_to_end(key)
        while len(self._pins) > self._cap:
            self._pins.popitem(last=False)

    def _locate(self, messages: list[dict], anchor: int, start: int, kept_sig: str) -> int:
        """Find the pinned head again: kept messages must survive, dropped ones may not.

        The check deliberately hashes the first *kept* message instead of the last dropped
        one. Clients rewrite old history in place (bounded tool results, truncation markers),
        so the message before the frontier changes regularly while the kept head does not;
        validating the dropped side reset the pin and re-cut the whole prefix every request.
        """
        if not kept_sig:
            return anchor  # no pin yet: start from the protected task header
        if start < len(messages) and message_sig(messages[start]) == kept_sig:
            return start
        candidates = [i for i in range(anchor, len(messages)) if message_sig(messages[i]) == kept_sig]
        if candidates:  # nearest surviving copy keeps the frontier where it was
            return min(candidates, key=lambda i: abs(i - start))
        self.resets += 1
        return anchor

    def apply(self, messages: list[dict], budget: int, low: int) -> tuple[list[dict], int]:
        """Return the pinned view of one conversation, cutting only when it outgrows `budget`."""
        if not messages:
            return messages, 0
        key = self.key(messages)
        anchor = _anchor(messages)
        start = self._locate(messages, anchor, *self._pins.get(key, (anchor, "")))
        kept_sig = message_sig(messages[start]) if start < len(messages) else ""
        head = messages[:anchor]
        view = messages[start:]
        if overhead_tokens(head) + overhead_tokens(view) <= budget:
            self._remember(key, (start, kept_sig))
            return (head + repair_tool_pairs(view)) if start > anchor else messages, 0
        kept, dropped = cut_prefix(messages, budget, low, anchor, start)
        if not dropped:
            # nothing cuttable is left before the current turn: forward the pinned view as-is
            self._remember(key, (start, kept_sig))
            return (head + repair_tool_pairs(view)) if start > anchor else messages, 0
        boundary = start + dropped
        self._remember(key, (boundary, message_sig(messages[boundary]) if boundary < len(messages) else ""))
        return repair_tool_pairs(kept), count_tool_blocks(messages[start:boundary])


FRONTIER = Frontier()


def drop_blocks_before_current_turn(messages: list[dict], budget: int = BUDGET) -> tuple[list, int]:
    """L1: strip media, then cut whole tool blocks before the current turn.

    Deliberately ignores the client's usage snapshot: it counts the history *before* this
    router trimmed it, so it stays above the budget for the rest of the session and would
    re-trim on every single request.
    """
    stripped, _ = strip_media(messages)
    return FRONTIER.apply(stripped, budget, int(budget * CUT_FRACTION))


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
