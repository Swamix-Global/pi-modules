"""Strategy tests: N-1 tool-pair shedding, condense offer, media stripping, whole-turn trim.

L1 sheds whole tool-call pairs oldest-first and keeps the newest one; prose is never cut.
That rule replaces the earlier prefix-cut/frontier scheme, which had to carry a low
watermark to stop the drop frontier from walking forward on every request.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


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


# --- tool-pair shedding (N-1) ------------------------------------------------------------
def pair_thread() -> list[dict]:
    return [{"role": "system", "content": "sys"},
            {"role": "user", "content": "turn one"},
            call("c1", "bash", "ls"), result("c1", "x" * 20000),
            {"role": "user", "content": "turn two"},
            call("c2", "bash", "grep"), result("c2", "y" * 20000),
            {"role": "user", "content": "current turn"}]


def test_oldest_pair_goes_first_and_prose_survives():
    messages = pair_thread()
    kept, dropped = u.drop_oldest_tool_pairs(messages, 6000)
    assert dropped == 2, "one call turn plus its result, as one unit"
    assert kept == [messages[0], messages[1], messages[4], messages[5], messages[6], messages[7]], \
        "both user turns and the current turn stay"
    assert call_ids(kept) == ["c2"], "the newest pair always survives"


def test_stops_as_soon_as_the_payload_fits():
    """No hysteresis band: shed only what the budget requires, never extra for headroom."""
    kept, _ = u.drop_oldest_tool_pairs(pair_thread(), 6000)
    assert call_ids(kept) == ["c2"]
    assert len(kept) == 6, "the older pair went, the newer one stayed"


def test_text_only_history_is_never_cut():
    messages = [{"role": "system", "content": "sys"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "w " * 4000} for i in range(6)]
    kept, dropped = u.drop_oldest_tool_pairs(messages, 100)
    assert kept is messages and dropped == 0, "prose is evidence, never droppable"


def test_the_newest_pair_goes_last_when_it_alone_cannot_fit():
    messages = [{"role": "user", "content": "go"},
                call("c1", "bash", "ls"), result("c1", "r" * 9000),
                call("c2", "bash", "ls"), result("c2", "r" * 9000),
                call("c3", "bash", "ls"), result("c3", "r" * 9000),
                {"role": "user", "content": "current"}]
    kept, dropped = u.drop_oldest_tool_pairs(messages, 10)
    assert dropped == 6, "all three pairs go, oldest first"
    assert kept == [messages[0], messages[7]], "prose survives even when every pair is shed"


def test_under_budget_returns_the_same_list():
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "go"}]
    kept, dropped = u.drop_oldest_tool_pairs(messages, 10_000)
    assert kept is messages and dropped == 0, "an untouched request keeps its exact bytes"


def test_is_pure():
    messages = [{"role": "user", "content": "go"}, call("c1", "bash", "ls"), result("c1", "r" * 500)]
    frozen = json.dumps(messages)
    u.drop_oldest_tool_pairs(messages, 10)
    assert json.dumps(messages) == frozen


def test_never_orphans_a_tool_result():
    messages = [{"role": "user", "content": "go"}, call("c1", "bash", "ls"),
                result("c1", "r" * 9000), {"role": "user", "content": "next"},
                call("c2", "bash", "ls"), result("c2", "r" * 9000)]
    kept, _ = u.drop_oldest_tool_pairs(messages, 100)
    kept_results = {m.get("toolCallId") for m in kept if m.get("role") == "toolResult"}
    assert kept_results == set(call_ids(kept)), "every kept result keeps its call turn"


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
