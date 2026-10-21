"""Strategy core tests: deterministic drop, condense-offer, and trim behavior."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import utils as u  # noqa: E402


def result(call_id: str, text: str) -> dict:
    return {"role": "toolResult", "toolCallId": call_id, "content": [{"type": "text", "text": text}]}


def call(call_id: str, name: str, args: str) -> dict:
    return {"role": "assistant", "content": [
        {"type": "toolCall", "id": call_id, "name": name, "arguments": {"command": args}}]}


def snapshot(body: dict, usage: int) -> str:
    return json.dumps(body) + f'\n\n_pi_ctx::{{"usage": {usage}}}'


# --- snapshot plumbing --------------------------------------------------------------
def test_snapshot_parse_roundtrip():
    parsed = u.parse_snapshot(snapshot({"messages": [{"role": "user", "content": "hi"}]}, 42000))
    assert parsed["usage"] == 42000
    assert parsed["body"]["messages"][0]["content"] == "hi"


def test_snapshot_missing_marker_raises():
    with pytest.raises(ValueError):
        u.parse_snapshot(json.dumps({"messages": []}))


def test_snapshot_malformed_json_raises():
    with pytest.raises(ValueError):
        u.parse_snapshot('{"messages": []}\n\n_pi_ctx::{"usage": "lots"}')


def call_ids(messages):
    return [b["id"] for m in messages if m.get("role") == "assistant"
            for b in m.get("content", []) if isinstance(b, dict) and b.get("type") == "toolCall"]


# --- strategy pass --------------------------------------------------------------------
def test_passes_drops_whole_blocks_atomically():
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
    dropped, kept, would = u.passes(messages, 100, 100)
    # c1's call and result drop together, c2's pair too (both needed to fit)
    assert {m.get("toolCallId") for m in dropped if m.get("role") == "toolResult"} == {"c1", "c2"}
    assert call_ids(dropped) == ["c1", "c2"]
    roles = [m.get("role") for m in kept]
    assert roles.count("system") == 1 and roles.count("user") == 3
    assert would and would[0] is messages[2]  # preview returns oldest victim


def test_passes_never_orphans_a_tool_result():
    messages = [{"role": "user", "content": "go"}, call("c1", "bash", "ls"), result("c1", "r" * 9000)]
    dropped, kept, _ = u.passes(messages, 100, 100)
    kept_results = {m.get("toolCallId") for m in kept if m.get("role") == "toolResult"}
    kept_calls = {b.get("id") for m in kept if m.get("role") == "assistant"
                  for b in m.get("content", []) if b.get("type") == "toolCall"}
    assert kept_results == kept_calls, "every kept result keeps its call turn"


def test_passes_is_pure():
    messages = [{"role": "user", "content": "go"}, call("c1", "bash", "ls"), result("c1", "r" * 500)]
    frozen = json.dumps(messages)
    u.passes(messages, 10, 10)
    assert json.dumps(messages) == frozen


def test_passes_skips_oversized_single_messages():
    monster = result("big", "z" * (int(u.MSG_TOKEN_LIMIT * u.CHARS_PER_TOKEN) + 500))
    messages = [{"role": "user", "content": "go"}, call("small", "bash", "ls"),
                result("small", "s" * 1000), monster]
    dropped, kept, _ = u.passes(messages, 100, 100)
    assert monster in kept, "oversized single message survives the drop pass"
    assert result("small", "s" * 1000) in dropped


def test_passes_stops_when_budget_met():
    messages = [{"role": "user", "content": "t1"}, call("c1", "bash", "ls"), result("c1", "r" * 5000),
                {"role": "user", "content": "t2"}, call("c2", "bash", "ls"), result("c2", "r" * 5000),
                {"role": "user", "content": "current"}]
    dropped, kept, _ = u.passes(messages, 2000, 2000)
    assert [m.get("toolCallId") for m in dropped if m.get("role") == "toolResult"] == ["c1"]
    assert result("c2", "r" * 5000) in kept  # newest tool block untouched


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


# --- SPEC: local_ai with durable file cache ------------------------------------------------
def test_local_ai_caches_by_input_hash(monkeypatch, tmp_path):
    calls = []

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            calls.append(1)
            return {"choices": [{"message": {"content": "cached-note"}}]}

    monkeypatch.setattr(u, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(u.requests, "post", lambda *a, **k: Resp())
    first = u.local_ai("sys prompt", "user text", 128)
    second = u.local_ai("sys prompt", "user text", 128)
    assert first == second == "cached-note"
    assert len(calls) == 1, "second identical call served from file cache"
    assert list(tmp_path.glob("*.txt")), "cache persisted to disk"


# --- SPEC: gzip transport ------------------------------------------------------------------
def test_maybe_gzip_only_for_cloud_providers_and_large_bodies():
    import gzip as gz
    big = b"x" * (u.GZIP_MIN_BYTES + 10)
    headers = {}
    body = u.maybe_gzip("deepseek", big, headers)
    assert headers["Content-Encoding"] == "gzip" and body != big
    headers2 = {}
    assert u.maybe_gzip("sglang", big, headers2) is big, "locals stay raw"
    headers3 = {}
    assert u.maybe_gzip("deepseek", b"small", headers3) == b"small", "small bodies stay raw"
    assert gz.decompress(body) == big
