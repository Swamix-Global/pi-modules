"""Condenser tests: the local model is asked once per unique input, then served from disk."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import condenser  # noqa: E402


def test_local_ai_caches_by_input_hash(monkeypatch, tmp_path):
    calls = []

    class Resp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            calls.append(1)
            return {"choices": [{"message": {"content": "cached-note"}}]}

    monkeypatch.setattr(condenser, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(condenser.requests, "post", lambda *a, **k: Resp())
    first = condenser.local_ai("sys prompt", "user text", 128)
    second = condenser.local_ai("sys prompt", "user text", 128)
    assert first == second == "cached-note"
    assert len(calls) == 1, "second identical call served from file cache"
    assert list(tmp_path.glob("*.txt")), "cache persisted to disk"


def test_condense_renders_every_wire_format(monkeypatch):
    seen = {}

    def fake_local_ai(system_prompt, user_message, max_tokens=2048):
        seen["prompt"], seen["text"] = system_prompt, user_message
        return "notes"

    monkeypatch.setattr(condenser, "local_ai", fake_local_ai)
    entries = [
        {"role": "assistant", "content": [{"type": "toolCall", "id": "c1", "name": "bash",
                                           "arguments": {"command": "ls"}}]},
        {"role": "toolResult", "toolCallId": "c1", "content": [{"type": "text", "text": "a.txt"}]},
    ]
    notes, _ = condenser.condense(entries, 512)
    assert notes == "notes"
    assert "toolCall bash" in seen["text"] and "a.txt" in seen["text"]
    assert "under ~" in seen["prompt"] and "words" in seen["prompt"]
    assert condenser.word_budget(512) == int(512 * 0.75)


def test_condense_enforces_the_word_budget(monkeypatch):
    monkeypatch.setattr(condenser, "local_ai",
                        lambda *a, **k: "word " * 50_000)
    notes, _ = condenser.condense([{"role": "user", "content": "hi"}], 512)
    assert condenser.count_words(notes) == condenser.word_budget(512)  # ellipsis rides the last word
