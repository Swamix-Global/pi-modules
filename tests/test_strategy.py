"""Strategy tests: contiguous prefix cut, condense offer, media stripping, whole-turn trim.

The old scattered "greedy drop anywhere" pass is gone on purpose: cutting non-contiguous
blocks out of the middle of history shifts the prompt head on every request, which is what
made the host re-bill whole prompts. `cut_prefix` only ever removes a contiguous prefix.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import strategy as u  # noqa: E402


def result(call_id: str, text: str) -> dict:
    return {"role": "toolResult", "toolCallId": call_id, "content": [{"type": "text", "text": text}]}


def call(call_id: str, name: str, args: str) -> dict:
    return {"role": "assistant", "content": [
        {"type": "toolCall", "id": call_id, "name": name, "arguments": {"command": args}}]}


def call_ids(messages):
    return [b["id"] for m in messages if m.get("role") == "assistant"
            for b in m.get("content", []) if isinstance(b, dict) and b.get("type") == "toolCall"]


# --- prefix cut -----------------------------------------------------------------------
def test_cut_prefix_drops_whole_blocks_atomically():
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "turn one"},
        call("c1", "bash", "ls"),
        result("c1", "x" * 20000),
        {"role": "user", "content": "turn two"},
        call("c2", "bash", "grep"),
        result("c2", "y" * 20000),
        {"role": "user", "content": "current turn"},
    ]
    kept, dropped = u.cut_prefix(messages, 100, 100)
    assert dropped == 5, "both call turns and both results go as one contiguous prefix"
    assert kept == [messages[0], messages[1], messages[7]]
    assert call_ids(kept) == [], "no call survives without its result"


def test_cut_prefix_under_budget_returns_the_same_list():
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    kept, dropped = u.cut_prefix(messages, 10_000, 4_000)
    assert kept is messages and dropped == 0, "an untouched request keeps its exact bytes"


def test_cut_prefix_never_orphans_a_tool_result():
    messages = [{"role": "user", "content": "go"}, call("c1", "bash", "ls"),
                result("c1", "r" * 9000), {"role": "user", "content": "next"},
                call("c2", "bash", "ls"), result("c2", "r" * 9000)]
    kept, dropped = u.cut_prefix(messages, 100, 100)
    assert dropped >= 2
    kept_results = {m.get("toolCallId") for m in kept if m.get("role") == "toolResult"}
    kept_calls = set(call_ids(kept))
    assert kept_results == kept_calls, "every kept result keeps its call turn"


def test_cut_prefix_is_pure():
    messages = [{"role": "user", "content": "go"}, call("c1", "bash", "ls"), result("c1", "r" * 500)]
    frozen = json.dumps(messages)
    u.cut_prefix(messages, 10, 10)
    assert json.dumps(messages) == frozen


def test_cut_prefix_stops_once_below_the_low_watermark():
    messages = [{"role": "user", "content": "t1"}, call("c1", "bash", "ls"), result("c1", "r" * 5000),
                {"role": "user", "content": "t2"}, call("c2", "bash", "ls"), result("c2", "r" * 5000),
                {"role": "user", "content": "current"}]
    kept, _ = u.cut_prefix(messages, 2000, 2000)
    assert result("c1", "r" * 5000) not in kept, "oldest tool block goes first"
    assert result("c2", "r" * 5000) in kept, "stop as soon as the cut reaches the watermark"
    assert kept[-1]["content"] == "current"


def test_cut_prefix_cuts_below_the_budget_for_headroom():
    """Low-water cutting is the hysteresis: landing on `low` buys turns of cache stability."""
    messages = [{"role": "user", "content": "t1"}, call("c1", "bash", "ls"), result("c1", "r" * 3600),
                {"role": "user", "content": "t2"}, call("c2", "bash", "ls"), result("c2", "r" * 3600),
                {"role": "user", "content": "current"}]
    kept_low, _ = u.cut_prefix(messages, 2000, 400)
    kept_budget, _ = u.cut_prefix(messages, 2000, 2000)
    assert u.overhead_tokens(kept_low) <= 400 < u.overhead_tokens(kept_budget)


# --- condense offer ---------------------------------------------------------------------
def test_condensed_prefix_is_everything_before_current_turn():
    messages = [{"role": "user", "content": "t1"}, call("c1", "bash", "ls"), result("c1", "r"),
                {"role": "user", "content": "current"}]
    entries, sigs = u.condensed_prefix_build(messages, [])
    assert entries == messages[:3]
    assert len(sigs) == 3


def test_identical_entries_never_condense_twice():
    messages = [{"role": "user", "content": "t1"}, call("c1", "bash", "ls"), result("c1", "r"),
                {"role": "user", "content": "current"}]
    entries, sigs = u.condensed_prefix_build(messages, [])
    again, sigs2 = u.condensed_prefix_build(messages, sigs)
    assert again == [] and sigs2 == sigs


def test_new_turns_extend_and_refold():
    messages = [{"role": "user", "content": "t1"}, call("c1", "bash", "ls"), result("c1", "r"),
                {"role": "user", "content": "current"}]
    _, sigs = u.condensed_prefix_build(messages, [])
    grown = messages + [call("c2", "bash", "ls"), result("c2", "r"), {"role": "user", "content": "next"}]
    entries, _ = u.condensed_prefix_build(grown, sigs)
    assert entries == grown[:6], "previous turn + new material extend the prefix and fold"


def test_single_turn_tool_dump_is_offered():
    messages = [{"role": "user", "content": "go"}, call("c1", "bash", "ls"), result("c1", "r" * 9000)]
    entries, _ = u.condensed_prefix_build(messages, [])
    assert entries == messages[-2:]


# --- pinned frontier -----------------------------------------------------------------
def frontier_thread(fill: int = 3000) -> list[dict]:
    return [{"role": "system", "content": "sys"}, {"role": "user", "content": "anchor"},
            call("c1", "bash", "ls"), result("c1", "x" * fill),
            {"role": "user", "content": "t2"}, call("c2", "bash", "ls"), result("c2", "y" * fill),
            {"role": "user", "content": "current"}]


def test_frontier_survives_client_rewrites_of_dropped_history():
    u.FRONTIER._pins.clear()
    view, dropped = u.FRONTIER.apply(frontier_thread(), 1000, 400)
    assert dropped == 4, "both call turns and their results are counted as dropped blocks"
    assert [m["content"] for m in view[:2]] == ["sys", "anchor"]

    drifted = frontier_thread()
    drifted[3] = result("c1", "rewritten in place by the client")  # dropped side changes
    drifted += [call("c3", "bash", "pwd"), result("c3", "r"), {"role": "user", "content": "next"}]
    again, re_dropped = u.FRONTIER.apply(drifted, 1000, 400)
    assert re_dropped == 0, "rewriting dropped history must not invalidate the pin"
    assert [m["content"] for m in again[:3]] == ["sys", "anchor", "current"]
    assert again[-1]["content"] == "next"


def test_frontier_recuts_when_the_kept_head_is_gone():
    u.FRONTIER._pins.clear()
    messages = frontier_thread()
    _, dropped = u.FRONTIER.apply(messages, 1000, 400)
    assert dropped == 4

    compacted = list(messages[:7])  # client rewrote or dropped the kept head message
    compacted[7:] = [{"role": "user", "content": "compacted turn"},
                     call("c4", "bash", "ls"), result("c4", "z" * 3000),
                     {"role": "user", "content": "next"}]
    _, re_dropped = u.FRONTIER.apply(compacted, 1000, 400)
    assert re_dropped > 0, "an unverifiable head means a fresh contiguous cut, never a stale one"


# --- safety-net trim ----------------------------------------------------------------------
def test_context_messages_preserve_tail_exactly():
    blocks = [
        {"type": "reasoning", "encrypted_content": "gAAAA-encrypted-bytes=="},
        {"type": "thinking", "thinking": "hmm", "signature": "sig=="},
        {"type": "text", "text": "answer"},
    ]
    messages = [{"role": "system", "content": "sys"},
                {"role": "user", "content": "t1"}, call("c1", "bash", "ls"), result("c1", "r" * 9000),
                {"role": "user", "content": "summarize"},
                {"role": "assistant", "content": blocks},
                {"role": "user", "content": "current"}]
    kept = u.context_messages(messages, 200)
    assert kept[0]["role"] == "system", "system always kept"
    assert kept[-1]["content"] == "current", "newest turn always sent"
    tail = next(m for m in kept if m.get("role") == "assistant")
    assert tail["content"] == blocks, "encrypted/thinking blocks survive byte-identical"


def test_context_messages_whole_turn_granularity():
    messages = [{"role": "user", "content": "t1"}, result("x", "r" * 5000),
                {"role": "user", "content": "t2"}, result("y", "r" * 5000),
                {"role": "user", "content": "t3"}]
    kept = u.context_messages(messages, 1600)
    assert kept == messages[2:], "never splits a turn"


def test_context_messages_keep_system_out_of_budget():
    messages = [{"role": "system", "content": "sys " * 400},
                {"role": "user", "content": "t1"}, result("x", "r" * 5000),
                {"role": "user", "content": "t3"}]
    kept = u.context_messages(messages, 20)
    assert kept[0]["role"] == "system" and kept[-1] == messages[-1]


# --- counters -------------------------------------------------------------------------------
def test_count_tool_blocks():
    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"},
                call("c1", "bash", "ls"), result("c1", "r"), result("c1", "r2"),
                {"role": "assistant", "content": [{"type": "text", "text": "done"}]}]
    assert u.count_tool_blocks(messages) == 3  # 1 call turn + 2 results


# --- SPEC: media stripping ---------------------------------------------------------------
def test_strip_media_removes_old_images_keeps_current_turn():
    img = {"type": "image", "mimeType": "image/png", "data": "AAAA"}
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": [{"type": "text", "text": "look"}, dict(img)]},
        call("c1", "bash", "ls"),
        {"role": "toolResult", "toolCallId": "c1",
         "content": [{"type": "image", "data": "BBBB"}, {"type": "text", "text": "shot"}]},
        {"role": "user", "content": [{"type": "text", "text": "and this one"}, dict(img)]},
    ]
    out, removed = u.strip_media(messages)
    assert removed == 2, "historical images go, current-turn images stay"
    assert out[1]["content"] == [{"type": "text", "text": "look"}]
    assert out[3]["content"] == [{"type": "text", "text": "shot"}]
    assert out[4] is messages[4], "current turn keeps its images untouched"
    assert messages[1]["content"] != out[1]["content"], "originals never mutate"
