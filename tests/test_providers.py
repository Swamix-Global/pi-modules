"""Provider compaction tests: full pipeline through the ASGI app, fake upstreams.

Covers deepseek-style tool chains, openai encrypted reasoning, antigravity
signed thinking, streaming, no-marker clients, condenser path and fail-open.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import condenser, strategy as u, transport as t  # noqa: E402
import backend.app as router  # noqa: E402
from backend.app import app  # noqa: E402
from backend.app import Stats  # noqa: E402


# --- fakes -----------------------------------------------------------------------------
class FakeResp:
    def __init__(self, payload: bytes, status: int = 200, ctype: str = "application/json"):
        self.payload, self.status_code = payload, status
        self.headers = {"content-type": ctype}

    def iter_content(self, size):
        for i in range(0, len(self.payload), size):
            yield self.payload[i:i + size]

    def json(self):
        return json.loads(self.payload)


def with_budget(monkeypatch, budget=400, level="L1"):
    monkeypatch.setattr(router, "BUDGET", budget)
    monkeypatch.setattr(router, "_prev_sigs", [])
    monkeypatch.setattr(router, "LEVEL", level)  # L1: drop-only; L2: drop + condense
    monkeypatch.setattr(router, "STATS", Stats())  # isolate counters per test
    u.FRONTIER._pins.clear()  # isolate the conversation frontier per test


def capture_upstream(monkeypatch, payload=None, status=200, ctype="application/json"):
    """Replace SESSION.post with a capture; returns the captured-dict accessor."""
    captured = {}

    def fake_post(url, data=None, headers=None, stream=False, timeout=None):
        captured["url"], captured["data"], captured["headers"] = url, data, headers
        body = payload if payload is not None else json.dumps(
            {"choices": [{"message": {"role": "assistant", "content": "ok"}}],
             "usage": {"prompt_tokens": 321}}).encode()
        return FakeResp(body, status, ctype)

    monkeypatch.setattr(router.SESSION, "post", fake_post)
    return captured


def client():
    return TestClient(app)


def ship(body: dict, usage: int = 50_000) -> bytes:
    return (json.dumps(body) + f"\n\n_pi_ctx::{{\"usage\": {usage}}}").encode()


def deepseek_thread(blocks=6, fill=3000):
    """Classic tool chain: user -> (assistant toolCall + result) x n -> current."""
    messages = [{"role": "system", "content": "you are pi"},
                {"role": "user", "content": "investigate the failure"}]
    for i in range(blocks):
        messages += [
            {"role": "assistant", "content": [
                {"type": "toolCall", "id": f"call_{i}", "name": "bash",
                 "arguments": {"command": f"grep -r error_{i}"}}]},
            {"role": "toolResult", "toolCallId": f"call_{i}",
             "content": [{"type": "text", "text": f"log line {i} " + "x" * fill}]},
        ]
    messages.append({"role": "user", "content": "now summarize the root cause"})
    return messages


def openai_thread():
    """openai responses-style: encrypted reasoning blocks must survive intact."""
    enc = "gAAAAAB" + "secret-bytes==" * 40
    return [
        {"role": "system", "content": "you are pi"},
        {"role": "user", "content": "why is the build red"},
        {"role": "assistant", "content": [
            {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": enc},
            {"type": "toolCall", "id": "call_a", "name": "bash", "arguments": {"command": "make"}},
        ]},
        {"role": "toolResult", "toolCallId": "call_a",
         "content": [{"type": "text", "text": "error: " + "e" * 4000}]},
        {"role": "assistant", "content": [
            {"type": "reasoning", "id": "rs_2", "summary": [], "encrypted_content": enc + "tail"},
            {"type": "text", "text": "the make target is broken"},
        ]},
        {"role": "user", "content": "fix it"},
    ]


def antigravity_thread():
    """gemini-style thinking blocks with signatures must survive intact."""
    return [
        {"role": "system", "content": "you are pi"},
        {"role": "user", "content": "scan the repo"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "I should grep first", "signature": "sig-abc=="},
            {"type": "toolCall", "id": "call_g", "name": "bash", "arguments": {"command": "ls"}},
        ]},
        {"role": "toolResult", "toolCallId": "call_g",
         "content": [{"type": "text", "text": "files: " + "f" * 4000}]},
        {"role": "user", "content": "go on"},
    ]


# --- deepseek-style tool chains ---------------------------------------------------------
def test_deepseek_chain_drops_oldest_blocks_keeps_current_turn(monkeypatch):
    with_budget(monkeypatch)
    captured = capture_upstream(monkeypatch)
    messages = deepseek_thread()
    r = client().post("/r/deepseek/chat/completions", content=ship({"messages": messages, "model": "deepseek-flash"}))
    assert r.status_code == 200
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    dropped = [m for m in messages if m not in shipped]
    assert any(m.get("role") == "toolResult" for m in dropped), "old tool results dropped"
    assert shipped[-1] == messages[-1], "current turn intact"
    assert shipped[0]["role"] == "system"
    kept_results = {m["toolCallId"] for m in shipped if m.get("role") == "toolResult"}
    kept_calls = {b["id"] for m in shipped if m.get("role") == "assistant"
                  for b in m.get("content", []) if b.get("type") == "toolCall"}
    assert kept_results == kept_calls, "no orphaned results after drop"
    assert captured["url"].endswith("/chat/completions")
    assert b"_pi_ctx" not in captured["data"], "router metadata never reaches the host"
    assert json.loads(captured["data"])["messages"], "shipped body parses clean upstream"


def test_deepseek_auth_injected_and_client_auth_stripped(monkeypatch):
    with_budget(monkeypatch, budget=10**9)  # stay under: no repair
    captured = capture_upstream(monkeypatch)
    monkeypatch.setattr(t, "AUTH_PATH", Path(__file__).parent / "fake-auth.json")
    (Path(__file__).parent / "fake-auth.json").write_text('{"deepseek": {"key": "sk-live-9"}}')
    try:
        r = client().post("/r/deepseek/chat/completions",
                          content=ship({"messages": [{"role": "user", "content": "hi"}]}),
                          headers={"Authorization": "Bearer client-key", "api-key": "client-key"})
    finally:
        (Path(__file__).parent / "fake-auth.json").unlink()
    assert r.status_code == 200
    assert captured["headers"]["Authorization"] == "Bearer sk-live-9"
    assert "api-key" not in captured["headers"] or captured["headers"]["api-key"] != "client-key"


def call_ids(messages):
    return [b["id"] for m in messages if m.get("role") == "assistant"
            for b in m.get("content", []) if isinstance(b, dict) and b.get("type") == "toolCall"]


# --- openai encrypted reasoning -----------------------------------------------------------
def test_openai_encrypted_reasoning_survives_repair(monkeypatch):
    """Old reasoning rides its droppable tool block out; newest reasoning stays verbatim."""
    with_budget(monkeypatch)
    captured = capture_upstream(monkeypatch)
    messages = openai_thread()
    r = client().post("/r/deepseek/chat/completions", content=ship({"messages": messages}))
    assert r.status_code == 200
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    encrypted = [b for m in shipped for b in m.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "reasoning"]
    assert len(encrypted) == 1, "old block gone with its toolCall; newest reasoning kept"
    assert encrypted[0]["encrypted_content"] == "gAAAAAB" + "secret-bytes==" * 40 + "tail"
    assert b"secret-bytes==" in captured["data"], "encrypted bytes verbatim on the wire"
    assert call_ids([m for m in messages if m not in shipped]) == ["call_a"], \
        "dropped call travels with its reasoning and result"


# --- antigravity signed thinking ------------------------------------------------------------
def test_antigravity_thinking_signature_intact(monkeypatch):
    """Nothing droppable is big enough to need repair: shipped untouched, signature whole."""
    with_budget(monkeypatch, budget=10**9)
    captured = capture_upstream(monkeypatch)
    messages = antigravity_thread()
    r = client().post("/r/antigravity/v1/chat/completions", content=ship({"messages": messages}))
    assert r.status_code == 200
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    think = [b for m in shipped for b in m.get("content", [])
             if isinstance(b, dict) and b.get("type") == "thinking"]
    assert len(think) == 1 and think[0]["signature"] == "sig-abc=="
    assert captured["url"].startswith("https://daily-cloudcode-pa.googleapis.com/")


def giant_history(fill=20000):
    """Over-budget material that the drop pass cannot remove: plain user text."""
    return [{"role": "system", "content": "you are pi"},
            {"role": "user", "content": "earlier context: " + "h" * fill},
            {"role": "user", "content": "now summarize the root cause"}]


# --- condenser path -----------------------------------------------------------------------
def test_history_overflow_condenses_via_sglang(monkeypatch):
    with_budget(monkeypatch, level="L2")
    captured = capture_upstream(monkeypatch)
    monkeypatch.setattr(condenser, "condense",
                        lambda entries, target: ("NOTES: user asked to scan; bash ls output huge", 12.3))
    messages = giant_history()
    r = client().post("/r/deepseek/chat/completions", content=ship({"messages": messages}))
    assert r.status_code == 200
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    assert shipped[0]["role"] == "system", "system prompt pinned verbatim"
    assert shipped[1]["content"].startswith("[context notes") and "NOTES" in shipped[1]["content"]
    assert shipped[-1] == messages[-1], "current turn survives condense"
    stats = client().get("/stats").json()
    assert stats["condenses"] >= 1 and stats["last_condense"]["trigger"] == "strategy+condenser"


def test_condense_fail_open_ships_trimmed_but_valid(monkeypatch):
    with_budget(monkeypatch, level="L2")
    captured = capture_upstream(monkeypatch)

    def boom(entries, target):
        raise RuntimeError("sglang down")

    monkeypatch.setattr(condenser, "condense", boom)
    messages = giant_history()
    r = client().post("/r/deepseek/chat/completions", content=ship({"messages": messages}))
    assert r.status_code == 200, "fail-open still serves the request"
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    assert shipped[0]["role"] == "system", "system prompt survives the safety trim"
    assert shipped[-1] == messages[-1], "current turn intact"
    stats = client().get("/stats").json()
    assert stats["condenses"] == 0, "failed condense is not counted"


def test_tool_dump_still_drops_before_condense(monkeypatch):
    """L1 policy: a droppable tool block closes the gap without any LLM call."""
    with_budget(monkeypatch, level="L1")
    captured = capture_upstream(monkeypatch)
    monkeypatch.setattr(condenser, "condense",
                        lambda entries, target: (_ for _ in ()).throw(AssertionError("condenser must not run")))
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "scan"},
                {"role": "assistant", "content": [{"type": "toolCall", "id": "c", "name": "bash",
                                                   "arguments": {"command": "ls"}}]},
                {"role": "toolResult", "toolCallId": "c",
                 "content": [{"type": "text", "text": "o" * 8000}]},
                {"role": "user", "content": "and then?"}]
    r = client().post("/r/deepseek/chat/completions", content=ship({"messages": messages}))
    assert r.status_code == 200
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    assert all(m.get("role") != "toolResult" for m in shipped), "dump dropped, no condense call"


# --- passthrough behaviors -----------------------------------------------------------------
def test_no_marker_client_passes_through_byte_identical(monkeypatch):
    captured = capture_upstream(monkeypatch)
    body = json.dumps({"messages": [{"role": "user", "content": "hi"}], "model": "local"}).encode()
    r = client().post("/r/lmstudio/v1/chat/completions", content=body)  # no _pi_ctx marker
    assert r.status_code == 200
    assert captured["data"] == body, "no snapshot -> zero mutation"
    assert r.headers["x-pi-router"] == "passthrough"


def test_under_budget_passes_through(monkeypatch):
    with_budget(monkeypatch, budget=10**9)
    captured = capture_upstream(monkeypatch)
    messages = deepseek_thread()
    r = client().post("/r/deepseek/chat/completions", content=ship({"messages": messages}, usage=120))
    assert r.status_code == 200
    assert captured["data"].startswith(b'{"messages"'), "rebuilt but not repaired"
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    assert shipped == messages


def test_streaming_request_streams_through(monkeypatch):
    with_budget(monkeypatch, budget=10**9)
    sse = b'data: {"delta": 1}\n\ndata: [DONE]\n\n'
    captured = capture_upstream(monkeypatch, payload=sse, ctype="text/event-stream")
    body = ship({"messages": [{"role": "user", "content": "hi"}], "stream": True})
    r = client().post("/r/deepseek/chat/completions", content=body)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert b"data: [DONE]" in r.content


def test_unknown_provider_404(monkeypatch):
    r = client().post("/r/nosuch/v1/chat/completions", content=b"{}")
    assert r.status_code == 404


def test_upstream_error_passes_status(monkeypatch):
    captured = capture_upstream(monkeypatch, payload=b'{"error": "quota"}', status=429)
    r = client().post("/r/deepseek/chat/completions",
                      content=ship({"messages": [{"role": "user", "content": "hi"}]}))
    assert r.status_code == 429


def test_stats_counters_move(monkeypatch):
    with_budget(monkeypatch)
    capture_upstream(monkeypatch)
    c = client()
    c.post("/r/deepseek/chat/completions", content=ship({"messages": deepseek_thread(blocks=3)}))
    stats = c.get("/stats").json()
    assert stats["requests"] >= 1 and stats["strategy_passes"] >= 1
    assert stats["dropped_blocks"] >= 1 and stats["usage_tokens"] > 0
    health = c.get("/health").json()
    assert set(health) == {"ok", "usage_tokens", "compactions", "active", "model"}


# --- OpenAI wire format (what Pi actually sends to openai-completions providers) -----------
def openai_wire_thread(fill=3000, blocks=4):
    messages = [{"role": "system", "content": "you are pi"},
                {"role": "user", "content": "investigate"}]
    for i in range(blocks):
        messages += [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"call_{i}", "type": "function",
                 "function": {"name": "bash", "arguments": json.dumps({"command": f"grep {i}"})}}]},
            {"role": "tool", "tool_call_id": f"call_{i}", "content": f"traceback {i}: " + "x" * fill},
        ]
    messages.append({"role": "user", "content": "summarize the root cause"})
    return messages


def test_openai_wire_format_drops_oldest_blocks_keeps_current_turn(monkeypatch):
    with_budget(monkeypatch)
    captured = capture_upstream(monkeypatch)
    messages = openai_wire_thread()
    r = client().post("/r/deepseek/chat/completions", content=ship({"messages": messages}))
    assert r.status_code == 200
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    dropped_ids = {m["tool_call_id"] for m in messages if m not in shipped and m.get("role") == "tool"}
    assert dropped_ids, "old OpenAI-format tool results dropped"
    kept_results = {m["tool_call_id"] for m in shipped if m.get("role") == "tool"}
    kept_calls = {c["id"] for m in shipped for c in m.get("tool_calls") or []}
    assert kept_results == kept_calls, "no orphaned results in OpenAI format"
    assert shipped[-1] == messages[-1]


def test_phantom_usage_claim_never_trims_an_under_budget_payload(monkeypatch):
    """A stale client snapshot (60k claimed, ~1k real) must not move the drop frontier.

    The snapshot counts the history before the router trimmed it, so it stays far above any
    budget for the rest of a session. Trimming on it rewrites the prompt head on every
    request and the host re-bills the whole tail - the cache-miss loop this guards.
    """
    with_budget(monkeypatch, level="L1")
    captured = capture_upstream(monkeypatch)
    messages = openai_wire_thread(fill=200, blocks=2)  # ~200 est, far under the 400 budget
    r = client().post("/r/deepseek/chat/completions",
                      content=ship({"messages": messages}, usage=60_000))
    assert r.status_code == 200
    shipped = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    assert shipped == messages, "payload is under budget: forwarded byte-identically"
    assert router.STATS.trims == 0


def test_drop_frontier_is_pinned_across_requests(monkeypatch):
    """One trim per budget band, not one per request: the head must stay fixed."""
    with_budget(monkeypatch, budget=1_000, level="L1")
    u.FRONTIER._pins.clear()
    messages = openai_wire_thread(fill=3_000, blocks=4)

    captured = capture_upstream(monkeypatch)
    assert client().post("/r/deepseek/chat/completions",
                         content=ship({"messages": messages})).status_code == 200
    first = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    assert router.STATS.trims == 1, "the first over-budget request trims"
    ids_first = [m.get("tool_call_id") for m in first if m.get("role") == "tool"]

    grown = messages[:-1] + [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_new", "type": "function",
             "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_new", "content": "y" * 400},
        messages[-1]]
    assert client().post("/r/deepseek/chat/completions",
                         content=ship({"messages": grown})).status_code == 200
    second = json.loads(captured["data"].decode().split("_pi_ctx::")[0])["messages"]
    ids_second = [m.get("tool_call_id") for m in second if m.get("role") == "tool"]
    assert router.STATS.trims == 1, "growth inside the band must not move the frontier"
    assert ids_second[:len(ids_first)] == ids_first, "cached head preserved across requests"
    kept_calls = {c["id"] for m in second for c in m.get("tool_calls") or []}
    assert set(ids_second) <= kept_calls, "no orphaned tool results"
