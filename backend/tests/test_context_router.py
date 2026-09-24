#!/usr/bin/env python3
"""Focused checks for the context router: pass-through, block condensation, cache determinism and SQLite
persistence, provider switch, dashboard/stats, image pass-through, oversized-turn trimming, background
condensation and streaming proxy behaviour."""
import compression.zstd as zstd
import gzip
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "ctx_engine_server.py"
SPEC = importlib.util.spec_from_file_location("context_router", MODULE_PATH)
router = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(router)
REAL_CALL_CONDENSER = router.call_condenser


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    body = b""
    authorization = ""
    content_encoding = ""
    path_seen = ""

    def log_message(self, _format, *_args):
        return

    def do_POST(self):
        type(self).path_seen = self.path
        type(self).authorization = self.headers.get("Authorization", "")
        type(self).content_encoding = self.headers.get("Content-Encoding", "")
        wire_body = self.rfile.read(int(self.headers["Content-Length"]))
        type(self).body = zstd.decompress(wire_body) if type(self).content_encoding == "zstd" else gzip.decompress(wire_body) if type(self).content_encoding == "gzip" else wire_body
        payload = (b'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
                   b'data: {"type":"response.completed","response":{"usage":{"input_tokens":1000,"output_tokens":20,"input_tokens_details":{"cached_tokens":400}}}}\n\n'
                   b'data: [DONE]\n\n')
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload[:20])
        self.wfile.flush()
        self.wfile.write(payload[20:])


def history_blocks(count=12):
    items = []
    for index in range(count):
        items.append({"role": "user", "content": [{"type": "input_text", "text": f"old request {index} " + "x" * 500}]})
        items.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"old answer {index} " + "y" * 500}]})
    return items


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ctx-router-test-")
        router.DB_PATH = os.path.join(self.tmpdir, "ctx-engine.db")
        router.LOCALAI_DB_PATH = os.path.join(self.tmpdir, "localai.db")
        with sqlite3.connect(router.LOCALAI_DB_PATH) as db:
            db.execute("create table requests(seq integer primary key, source text, model text, status text, input_tokens integer, output_tokens integer, reasoning_tokens integer, cached_tokens integer, elapsed_ms integer, started_at integer)")
            db.execute("insert into requests values(1, 'subagentx-compress', 'test-model', 'ok', 100, 20, 5, 40, 50, 1700000000000)")
        for key in list(router.METRICS):
            if isinstance(router.METRICS[key], (int, float)) and key != "started":
                router.METRICS[key] = 0
        router.WAIT_MS = 2000
        router.BUDGET["tokens"] = router.CONTEXT_TOKENS
        router.SUMMARY["tokens"] = router.DEFAULT_SUMMARY_TOKENS
        with router.JOBS_LOCK:
            router.SUMMARY_JOBS.clear()
        self.calls = []

    def tearDown(self):
        router.call_condenser = REAL_CALL_CONDENSER
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def stub_condenser(self, reply="- Task/Query: fix checkout\n- Actions Performed: patched the retry path\n- Conclusion: checkout still failed after r221; error rate peaked at 42.0 percent."):
        def fake(prompt, _target=None):
            self.calls.append(prompt)
            return reply
        router.call_condenser = fake

    def test_below_threshold_is_byte_identical(self):
        raw = b'{ "model": "gpt", "input": [{"role":"user","content":"hello"}] }'
        output, changed = router.rewrite_request(raw, token_limit=64000)
        self.assertFalse(changed)
        self.assertEqual(output, raw)

    def test_image_content_passes_through_byte_identical(self):
        raw = json.dumps({"input": [{"role": "user", "content": [{"type": "input_text", "text": "inspect"}, {"type": "input_image", "image_url": "data:image/png;base64,AA=="}]}]}).encode()
        output, changed = router.rewrite_request(raw)
        self.assertFalse(changed)
        self.assertEqual(output, raw)
        self.assertEqual(router.stats_snapshot()["images"], 1)

    def test_image_payload_does_not_count_as_context(self):
        """A base64 screenshot must not inflate the text budget and trigger compaction on its own."""
        body = {"messages": [{"role": "user", "content": [{"type": "text", "text": "see screenshot"},
                                                      {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 200_000}}]}]}
        self.assertLess(router.estimate_chars(body), 1000)
        without_image = {"messages": [{"role": "user", "content": [{"type": "text", "text": "see screenshot"}]}]}
        self.assertEqual(router.estimate_tokens(body), router.estimate_tokens(without_image), "the image payload is excluded from the token estimate too")
        output, changed = router.rewrite_request(json.dumps(body).encode(), token_limit=1000)
        self.assertFalse(changed)
        self.assertEqual(output, json.dumps(body).encode())

    def test_estimate_tokens_uses_the_chars_per_token_fallback(self):
        """The router's single budget unit; with no tokenizer in-process text is 3.5 chars per token."""
        self.assertEqual(router.CHARS_PER_TOKEN, 3.5)
        self.assertEqual(router.estimate_tokens("x" * 350), 100)
        self.assertEqual(router.estimate_tokens("x" * 3500), 1000)
        self.assertEqual(router.estimate_tokens(b"x" * 350), 100)
        self.assertEqual(router.estimate_tokens(""), 0)

    def test_status_exposes_token_equivalents_marked_as_estimates(self):
        """The char counters stay for compatibility; the dashboard gets token equivalents derived at 3.5 chars/token."""
        router.bump(input_chars_raw=3500, input_chars_sent=700)
        snapshot = router.stats_snapshot()
        self.assertEqual(snapshot["input_chars_raw"], 3500)
        self.assertEqual(snapshot["input_chars_sent"], 700)
        self.assertEqual(snapshot["chars_per_token"], router.CHARS_PER_TOKEN)
        self.assertEqual(snapshot["input_tokens_raw_est"], 1000)
        self.assertEqual(snapshot["input_tokens_sent_est"], 200)

    def test_compacting_a_block_strips_its_image(self):
        self.stub_condenser()
        image = {"role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64," + "A" * 4096}]}
        items = history_blocks(3) + [image, {"role": "assistant", "content": [{"type": "output_text", "text": "seen"}]},
                                     {"role": "user", "content": [{"type": "input_text", "text": "current"}]}]
        output, changed = router.rewrite_request(json.dumps({"input": items}).encode(), token_limit=1000)
        self.assertTrue(changed)
        self.assertNotIn(b"base64", output, "a compacted block must lose its image payload")
        self.assertEqual(json.loads(output)["input"][-1], items[-1], "the live turn stays byte-exact")
        self.assertTrue(all("base64" not in prompt for prompt in self.calls), "image payload must never reach the condenser")
        self.assertTrue(any("[image]" in prompt for prompt in self.calls), "the summary still records that an image was there")

    def test_condenser_includes_summary_length_hint(self):
        captured = {}
        real_http, real_key = router.http_json, router.deepseek_key
        try:
            router.deepseek_key = lambda: "test-key-not-real"
            router.http_json = lambda url, body, headers, timeout: captured.update(body) or {"choices": [{"message": {"content": "- Task/Query: clipped\n- Actions Performed: none\n- Conclusion: none"}}]}
            summary = router.call_condenser("x" * 5000)
        finally:
            router.http_json, router.deepseek_key = real_http, real_key
        self.assertIn("Length hint: this block is", captured["messages"][-1]["content"])
        self.assertIn("within 100 tokens.", captured["messages"][-1]["content"], "the global token ceiling is the hint")
        self.assertTrue(router.is_current_summary(summary))

    def test_summary_length_is_part_of_the_cache_key(self):
        prose = "- Task/Query: a\n- Actions Performed: b\n- Conclusion: c"
        router.cache_put("d1", prose, "local", 100)
        self.assertEqual(router.cache_get("d1", "local", 100), prose)
        self.assertIsNone(router.cache_get("d1", "local", 200), "a different length target must not hit")

    def test_every_length_coexists_in_the_cache(self):
        notes = {step: f"- Task/Query: note {step}\n- Actions Performed: b\n- Conclusion: c" for step in router.SUMMARY_TOKEN_STEPS}
        for step, note in notes.items():
            router.cache_put("coexist", note, "deepseek", step)
        for step, note in notes.items():
            self.assertEqual(router.cache_get("coexist", "deepseek", step), note, f"length {step} answers itself")
        self.assertEqual(router.cache_counts().get("deepseek"), 4, "all four lengths persist side by side")
        conn = sqlite3.connect(router.DB_PATH)
        try:
            rows = conn.execute("SELECT tokens FROM summaries WHERE digest = 'coexist' ORDER BY tokens").fetchall()
        finally:
            conn.close()
        self.assertEqual([int(r[0]) for r in rows], [50, 100, 200, 400])

    def test_no_cache_hit_across_a_condenser_model_change(self):
        note = "- Task/Query: a\n- Actions Performed: b\n- Conclusion: c"
        router.cache_put("m1", note, "deepseek", 100, "model-a")
        self.assertEqual(router.cache_get("m1", "deepseek", 100, "model-a"), note)
        self.assertIsNone(router.cache_get("m1", "deepseek", 100, "model-b"), "a switched model must not read the old note")
        router.cache_put("m1", note.replace("a", "z"), "deepseek", 100, "model-b")
        self.assertEqual(router.cache_get("m1", "deepseek", 100, "model-a"), note, "the old model's row is preserved")
        conn = sqlite3.connect(router.DB_PATH)
        try:
            models = sorted(str(r[0]) for r in conn.execute("SELECT model FROM summaries WHERE digest = 'm1'"))
        finally:
            conn.close()
        self.assertEqual(models, ["model-a", "model-b"], "both models persist side by side")

    def test_summary_lengths_come_from_the_fixed_token_menu(self):
        for tokens in router.SUMMARY_TOKEN_STEPS:
            router.SUMMARY["tokens"] = tokens
            self.assertEqual(router.summary_target(), tokens)
            self.assertTrue(router.summary_hint("x" * 350).endswith(f"keep the whole note within {tokens} tokens."))
        self.assertEqual(router.SUMMARY_TOKEN_STEPS, (50, 100, 200, 400), "token-first menu, not per-block word steps")
        router.SUMMARY["tokens"] = 100
        self.assertEqual(router.summary_target(), 100, "one global length, not derived from the block size")

    def test_old_word_rows_are_not_reusable_as_token_entries(self):
        conn = sqlite3.connect(router.DB_PATH)
        conn.execute("CREATE TABLE summaries (digest TEXT NOT NULL, provider TEXT NOT NULL, summary TEXT NOT NULL, "
                     "model TEXT NOT NULL, created_at REAL NOT NULL, words INTEGER NOT NULL DEFAULT 0, "
                     "PRIMARY KEY (digest, provider))")
        conn.execute("INSERT INTO summaries VALUES ('d1', 'local', '- Task/Query: old word note', 'm', 1.0, 100)")
        conn.execute("INSERT INTO summaries VALUES ('d2', 'local', '- Task/Query: old short note', 'm', 1.0, 500)")
        conn.commit()
        conn.close()
        self.assertIsNone(router.cache_get("d1", "local", 100), "an old words row must never answer a token request")
        self.assertIsNone(router.cache_get("d2", "local", 400))
        conn = sqlite3.connect(router.DB_PATH)
        try:
            cols = [str(r[1]) for r in conn.execute("PRAGMA table_info(summaries)")]
            self.assertIn("tokens", cols)
            self.assertNotIn("words", cols)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM summaries").fetchone()[0], 0, "word rows are never reused")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM summaries_words_legacy").fetchone()[0], 2, "old notes are archived")
        finally:
            conn.close()

    def test_superseded_schema_is_archived_not_dropped(self):
        """A pre-token-pk table that still has token lengths keeps its notes: archived, and copied into the new cache."""
        conn = sqlite3.connect(router.DB_PATH)
        conn.execute("CREATE TABLE summaries (digest TEXT NOT NULL, provider TEXT NOT NULL, summary TEXT NOT NULL, "
                     "model TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, "
                     "PRIMARY KEY (digest, provider))")
        conn.execute("INSERT INTO summaries VALUES ('keep', 'local', '- Task/Query: preserved', 'm', 100, 1.0)")
        conn.execute("INSERT INTO summaries VALUES ('off-menu', 'local', '- Task/Query: bad length', 'm', 123, 1.0)")
        conn.commit()
        conn.close()
        router.init_db()
        conn = sqlite3.connect(router.DB_PATH)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM summaries_schema_legacy").fetchone()[0], 2, "the old table survives")
            rows = dict(conn.execute("SELECT digest, tokens FROM summaries").fetchall())
            self.assertEqual(rows, {"keep": 100}, "only menu-length token notes are carried forward")
            self.assertEqual(router.cache_get("keep", "local", 100, "m"), "- Task/Query: preserved")
        finally:
            conn.close()

    def test_dns_blip_is_retried_once(self):
        class Flaky:
            def __init__(self):
                self.calls = 0

            def request(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise socket.gaierror(-3, "Temporary failure in name resolution")

        conn = Flaky()
        router.connect_and_send(conn, "POST", "/x", b"{}", {})
        self.assertEqual(conn.calls, 2)

    def test_transcript_keeps_only_core_schema(self):
        """Harness events never reach the condenser as JSON: a toolResult is text, an unknown event is dropped."""
        block = [{"role": "toolResult", "toolCallId": "c1", "toolName": "bash", "isError": False, "content": [{"type": "text", "text": "ok"}]},
                 {"type": "telemetry", "at": 1}]
        self.assertEqual(router.block_transcript(block), "tool output: ok")

    def test_blocks_are_deterministic_and_the_current_turn_is_exact(self):
        self.stub_condenser()
        current = [
            {"role": "user", "content": [{"type": "input_text", "text": "current request"}]},
            {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": '{"path":"x"}'},
            {"type": "function_call_output", "call_id": "call-1", "output": "result"},
        ]
        body = {"instructions": "system", "tools": [{"type": "function", "name": "read"}], "input": history_blocks() + current}
        output, changed = router.rewrite_request(json.dumps(body).encode(), token_limit=1000)
        self.assertTrue(changed)
        rewritten = json.loads(output)
        self.assertEqual(rewritten["input"][-len(current):], current)
        self.assertTrue(rewritten["input"][0]["content"][0]["text"].startswith(router.FRAME))
        self.assertLessEqual(router.estimate_tokens(rewritten), 1000)
        # one summary per completed block while they fit; oldest are dropped first, never the current turn
        summaries = rewritten["input"][:-len(current)]
        self.assertTrue(1 <= len(summaries) <= 12, len(summaries))
        ids = [re.search(r"\[block ([0-9a-f]+)\]", s["content"][0]["text"]).group(1) for s in summaries]
        self.assertEqual(len(set(ids)), len(ids), "block ids must be unique")
        self.assertTrue(all("[block " in s["content"][0]["text"] for s in summaries))

    def test_repeated_blocks_are_summarized_once(self):
        self.stub_condenser()
        body = {"input": history_blocks(6) + [{"role": "user", "content": [{"type": "input_text", "text": "next"}]}]}
        first = router.rewrite_request(json.dumps(body).encode(), token_limit=1000)
        calls_after_first = len(self.calls)
        second = router.rewrite_request(json.dumps(body).encode(), token_limit=1000)
        self.assertTrue(first[1] and second[1])
        self.assertEqual(len(self.calls), calls_after_first, "a repeated block must be served from cache")
        self.assertGreater(router.stats_snapshot()["cache_hits"], 0)

    def test_condenser_failure_never_releases_the_uncompacted_turn(self):
        def broken(_prompt, _target=None):
            raise router.CondenserError("backend down")
        real = router.call_condenser
        router.call_condenser = broken
        try:
            raw = json.dumps({"input": history_blocks(6) + [{"role": "user", "content": [{"type": "input_text", "text": "next"}]}]}).encode()
            output, changed = router.rewrite_request(raw, token_limit=1000)
            self.assertTrue(changed, "a failed condenser must not release the whole backlog")
            self.assertNotIn(b"old request 0", output)
            self.assertLessEqual(router.estimate_tokens(json.loads(output)), 1000)
            stats = router.stats_snapshot()
            self.assertEqual(stats["condenser_errors"], 6)
            self.assertEqual(stats["blocks_pending"], 6)
        finally:
            router.call_condenser = real

    def test_summaries_persist_across_a_restart(self):
        self.stub_condenser()
        items = history_blocks(6)
        body = {"input": items + [{"role": "user", "content": [{"type": "input_text", "text": "next"}]}]}
        router.rewrite_request(json.dumps(body).encode(), token_limit=1000)
        digest = router.block_id(router.split_blocks(items)[0])
        self.assertIsNotNone(router.cache_get(digest, "deepseek"))
        # a fresh process (module reload) reading the same ctx-engine.db serves the same summary from cache
        spec = importlib.util.spec_from_file_location("context_router_restart", MODULE_PATH)
        fresh = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(fresh)
        fresh.DB_PATH = router.DB_PATH
        self.assertEqual(fresh.cache_get(digest, "deepseek"), router.cache_get(digest, "deepseek"))
        self.assertGreaterEqual(fresh.cache_count(), 1)
        self.assertEqual(fresh.stats_snapshot()["cache_db"], router.DB_PATH)

    def test_slider_thresholds_persist_across_a_restart(self):
        """The budget and block-length sliders survive a restart: they are written to ctx-engine.db, and a
        fresh process loads the chosen values over the env defaults instead of reverting."""
        self.assertTrue(router.init_db())
        router.BUDGET["tokens"] = 100000
        router.SUMMARY["tokens"] = 200
        router.config_set("tokens", 100000)
        router.config_set("summary_tokens", 200)
        spec = importlib.util.spec_from_file_location("context_router_sliders", MODULE_PATH)
        fresh = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(fresh)
        fresh.DB_PATH = router.DB_PATH
        fresh.BUDGET["tokens"] = fresh.CONTEXT_TOKENS
        fresh.SUMMARY["tokens"] = fresh.DEFAULT_SUMMARY_TOKENS
        fresh.load_config()
        self.assertEqual(fresh.BUDGET["tokens"], 100000)
        self.assertEqual(fresh.stats_snapshot()["token_limit"], 100000)
        self.assertEqual(fresh.stats_snapshot()["summary_tokens"], 200)

    def test_live_config_writes_through_to_disk(self):
        """The live /config endpoint must persist, not only move the in-memory dict, so the slider is durable."""
        server = router.make_server("127.0.0.1", 0, "https://upstream.invalid")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            client.request("POST", "/config", body=b'{"tokens":64000,"summary_tokens":400}', headers={"Content-Type": "application/json"})
            ok = client.getresponse()
            self.assertEqual(ok.status, 200)
            ok.read()
            client.close()
        finally:
            server.shutdown()
            server.server_close()
        spec = importlib.util.spec_from_file_location("context_router_sliders_http", MODULE_PATH)
        fresh = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(fresh)
        fresh.DB_PATH = router.DB_PATH
        fresh.load_config()
        self.assertEqual(fresh.BUDGET["tokens"], 64000)
        self.assertEqual(fresh.SUMMARY["tokens"], 400)

    def test_deepseek_empty_content_is_retried_once(self):
        """A reasoning model can spend the whole budget on reasoning and return empty content; one retry at
        double the budget must recover it, and a second empty answer must not loop."""
        real_http, real_key = router.http_json, router.deepseek_key
        try:
            router.deepseek_key = lambda: "test-key-not-real"
            replies = [{"choices": [{"message": {"content": "", "reasoning_content": "thinking"}}]},
                       {"choices": [{"message": {"content": "the summary"}}]}]
            seen = []
            def fake_http(url, payload, headers, timeout):
                seen.append(payload["max_tokens"])
                return replies.pop(0)
            router.http_json = fake_http
            self.assertEqual(router.call_condenser("a block"), "- the summary")
            self.assertEqual(seen, [router.DEEPSEEK_MAX_TOKENS, router.DEEPSEEK_MAX_TOKENS * 2])
            router.http_json = lambda *a, **k: {"choices": [{"message": {"content": ""}}]}
            with self.assertRaisesRegex(router.CondenserError, "no usable text"):
                router.call_condenser("a block")
        finally:
            router.http_json, router.deepseek_key = real_http, real_key

    def test_condenser_sampling_and_labelled_sections(self):
        for name in ("Task/Query:", "Actions Performed:", "Conclusion:"):
            self.assertIn(name, router.CONDENSER_SYSTEM)
        labelled = router.normalize_summary("Task/Query: a Actions Performed: b Conclusion: c")
        self.assertEqual(labelled, "- Task/Query: a\n- Actions Performed: b\n- Conclusion: c")
        self.assertTrue(router.is_current_summary(labelled))
        self.assertFalse(router.is_current_summary("- Task: a\n- Work done: b\n- Results: c"))
        real_http, real_key = router.http_json, router.deepseek_key
        try:
            router.deepseek_key = lambda: "test-key-not-real"
            captured = {}
            router.http_json = lambda url, body, headers, timeout: captured.update(body) or {"choices": [{"message": {"content": "- Task: x"}}]}
            self.assertEqual(router.call_condenser("x" * 350), "- Task: x")
            self.assertEqual(captured["temperature"], router.DEEPSEEK_TEMPERATURE)
            self.assertNotIn("min_p", captured)
            self.assertNotIn("repetition_penalty", captured)
            self.assertTrue(captured["messages"][-1]["content"].endswith(
                "Length hint: this block is about 100 tokens; keep the whole note within 100 tokens."))
        finally:
            router.http_json, router.deepseek_key = real_http, real_key

    def test_prose_and_stale_cache_are_forced_back_to_bullets(self):
        """The DeepSeek model ignores the format prompt, so the shape is enforced in code and stale prose is not a cache hit."""
        self.assertEqual(router.normalize_summary("Task: a Work done: b Results: c"), "- Task: a\n- Work done: b\n- Results: c")
        self.assertEqual(router.normalize_summary("* 1. Task: a"), "- Task: a")
        self.stub_condenser("- fresh bullet")
        items = history_blocks(6)
        digest = router.block_id(router.split_blocks(items)[0])
        router.cache_put(digest, "old prose paragraph", "deepseek")
        body = {"input": items + [{"role": "user", "content": [{"type": "input_text", "text": "next"}]}]}
        router.rewrite_request(json.dumps(body).encode(), token_limit=1000)
        self.assertEqual(router.cache_get(digest, "deepseek"), "- fresh bullet")

    def test_each_backend_has_its_own_cache_bucket(self):
        router.cache_put("d1", "- local", "local")
        router.cache_put("d1", "- deepseek", "deepseek")
        self.assertEqual(router.cache_get("d1", "local"), "- local")
        self.assertEqual(router.cache_get("d1", "deepseek"), "- deepseek")
        counts = router.stats_snapshot()["cache_buckets"]
        self.assertGreaterEqual(counts["local"], 1)
        self.assertGreaterEqual(counts["deepseek"], 1)

    def test_chat_completions_is_condensed_and_the_live_turn_is_exact(self):
        self.stub_condenser()
        messages = [{"role": "system", "content": "kernel instructions"}]
        for index in range(8):
            messages.append({"role": "user", "content": f"old ask {index} " + "x" * 4000})
            messages.append({"role": "assistant", "content": f"old answer {index}"})
        live = [
            {"role": "user", "content": "current ask"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "read", "arguments": '{"path":"x"}'}}]},
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ]
        body = {"model": "deepseek-flash", "messages": messages + live}
        output, changed = router.rewrite_request(json.dumps(body).encode(), token_limit=1000)
        self.assertTrue(changed)
        rewritten = json.loads(output)
        self.assertEqual(rewritten["messages"][0], {"role": "system", "content": "kernel instructions"})
        self.assertEqual(rewritten["messages"][-len(live):], live, "live turn must stay byte-exact")
        summaries = rewritten["messages"][1:-len(live)]
        self.assertTrue(summaries and all(m["role"] == "user" and router.FRAME in m["content"] for m in summaries))
        self.assertTrue(all(re.search(r"\[block [0-9a-f]{16}\]", m["content"]) for m in summaries))
        self.assertLessEqual(router.estimate_tokens(rewritten), 1000)
        # a tool result outside a block would break the API contract, so the tool message must follow its call inside the live turn
        self.assertEqual([m["role"] for m in rewritten["messages"][-3:]], ["user", "assistant", "tool"])

    def test_provider_route_prefix_maps_to_its_backend(self):
        server = router.make_server("127.0.0.1", 0, "https://upstream.invalid")
        handler = router.RouterHandler
        handler.server = server
        try:
            self.assertEqual(server.resolve("x") if False else router.RouterHandler.resolve_upstream(handler, "/codex/responses"), ("https://upstream.invalid", "/codex/responses"))
            upstream, rest = router.RouterHandler.resolve_upstream(handler, "/r/deepseek/chat/completions")
            self.assertEqual((upstream, rest), (router.ROUTES["deepseek"], "/chat/completions"))
            self.assertEqual(router.RouterHandler.resolve_upstream(handler, "/r/nope/chat/completions"), (None, "/r/nope/chat/completions"))
        finally:
            server.server_close()

    def test_oversized_turn_is_trimmed_to_the_budget(self):
        raw = json.dumps({"input": [{"role": "user", "content": [{"type": "input_text", "text": "x" * 5000}]}]}).encode()
        output, changed = router.rewrite_request(raw, token_limit=200)
        self.assertTrue(changed, "the engine must apply the budget even with nothing to condense")
        rewritten = json.loads(output)
        self.assertLessEqual(router.estimate_tokens(rewritten), 200)
        self.assertEqual(rewritten["input"][0]["role"], "user", "structure survives the trim")
        self.assertIn("…", rewritten["input"][0]["content"][0]["text"], "the cut is marked")
        stats = router.stats_snapshot()
        self.assertEqual(stats["trimmed"], 1)
        self.assertEqual(stats["overflow"], 0)

    def test_trimming_never_touches_an_image_payload(self):
        image = {"type": "input_image", "image_url": "data:image/png;base64," + "A" * 20000}
        body = {"input": [{"role": "user", "content": [{"type": "input_text", "text": "x" * 5000}, image]}]}
        output, changed = router.rewrite_request(json.dumps(body).encode(), token_limit=200)
        self.assertTrue(changed)
        self.assertEqual(json.loads(output)["input"][0]["content"][1], image, "the image is passed byte-identical")

    def test_slow_condenser_is_held_back_then_cached(self):
        release = threading.Event()

        def slow(prompt, _target=None):
            self.calls.append(prompt)
            release.wait(5)
            return "- Task/Query: slow\n- Actions Performed: waited\n- Conclusion: cached for the next request"

        real_wait, real_call = router.WAIT_MS, router.call_condenser
        router.call_condenser, router.WAIT_MS = slow, 50
        try:
            raw = json.dumps({"input": history_blocks(6) + [{"role": "user", "content": [{"type": "input_text", "text": "next"}]}]}).encode()
            first, changed = router.rewrite_request(raw, token_limit=1000)
            self.assertTrue(changed)
            self.assertIn(router.PENDING_SUMMARY, first.decode(), "an unfinished note is held back, not sent as history")
            self.assertEqual(router.stats_snapshot()["blocks_pending"], 6)
            release.set()
            deadline = time.time() + 10
            while router.cache_count() < 6 and time.time() < deadline:
                time.sleep(0.05)
            self.assertEqual(router.cache_count(), 6, "the workers finish the notes for the next request")
            calls = len(self.calls)
            second, _changed = router.rewrite_request(raw, token_limit=1000)
            self.assertNotIn(router.PENDING_SUMMARY, second.decode(), "the next request reads the cached notes")
            self.assertIn("cached for the next request", second.decode())
            self.assertEqual(len(self.calls), calls, "cached notes are not recomputed")
        finally:
            release.set()
            router.call_condenser, router.WAIT_MS = real_call, real_wait

    def test_compressed_requests_decode_without_changing_small_bodies(self):
        raw = b'{"input":[{"role":"user","content":"hello"}]}'
        for compressed, decompress in ((gzip.compress(raw, mtime=0), gzip.decompress), (zstd.compress(raw), zstd.decompress)):
            self.stub_condenser()
            output, changed = router.rewrite_request(decompress(compressed), token_limit=64000)
            self.assertFalse(changed)
            self.assertEqual(output, raw)

    def test_http_proxy_forwards_auth_body_path_and_sse(self):
        self.stub_condenser()
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        proxy = router.make_server("127.0.0.1", 0, f"http://127.0.0.1:{upstream.server_port}/backend-api")
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        try:
            client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            client.request("GET", "/health")
            health = client.getresponse()
            self.assertEqual((health.status, health.read()), (200, b'{"status":"ok"}'))
            client.close()

            client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            client.request("GET", "/dashboard")
            page = client.getresponse()
            body = page.read().decode()
            self.assertEqual(page.status, 200)
            self.assertIn("Context Router", body)
            self.assertIn('/dashboard/assets/', body)
            client.close()

            client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            client.request("GET", "/token-stats")
            token_response = client.getresponse()
            self.assertEqual(token_response.status, 200)
            self.assertEqual(json.loads(token_response.read())["totals"]["input_tokens"], 100)
            client.close()

            raw = b'{"input":[{"role":"user","content":"hello"}]}'
            client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            client.request("POST", "/codex/responses?probe=1", body=raw, headers={"Content-Type": "application/json", "Authorization": "Bearer test-only"})
            response = client.getresponse()
            payload = response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Context-Router"), "pass")
            self.assertIn(b'data: [DONE]', payload)
            self.assertEqual(UpstreamHandler.path_seen, "/backend-api/codex/responses?probe=1")
            self.assertEqual(UpstreamHandler.authorization, "Bearer test-only")
            self.assertEqual(UpstreamHandler.body, raw)
            client.close()

            filler = int(router.CONTEXT_TOKENS * router.CHARS_PER_TOKEN) + 200_000
            big = {"role": "user", "content": [{"type": "input_text", "text": "x" * filler}]}
            answer = {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "noted"}]}
            large = json.dumps({"input": [big, answer, {"role": "user", "content": [{"type": "input_text", "text": "current"}]}]}).encode()
            large_body = json.loads(large)
            compressed = zstd.compress(large)
            client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            client.request("POST", "/codex/responses", body=compressed, headers={"Content-Type": "application/json", "Content-Encoding": "zstd"})
            response = client.getresponse()
            response.read()
            self.assertEqual(response.status, 200)
            self.assertEqual(response.getheader("X-Context-Router"), "compacted")
            self.assertEqual(UpstreamHandler.content_encoding, "zstd")
            rewritten = json.loads(UpstreamHandler.body)
            self.assertTrue(rewritten["input"][0]["content"][0]["text"].startswith(router.FRAME))
            self.assertLessEqual(router.estimate_tokens(rewritten), router.CONTEXT_TOKENS)

            row = router.recent_requests(1)[0]
            self.assertEqual(row["cost"]["in"], 600, "proxied response usage is billed against the request")
            self.assertEqual(row["cost"]["cached"], 400)
            self.assertEqual(row["cost"]["out"], 20)
            client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            client.request("GET", f"/request/{row['id']}")
            detail = json.loads(client.getresponse().read())
            client.close()
            self.assertEqual(detail["id"], row["id"])
            self.assertEqual(detail["result"], "compacted")
            self.assertEqual([b["id"] for b in detail["plan"]["blocks"]], [router.block_id(b) for b in router.split_blocks(large_body["input"])[:-1]])
            self.assertEqual(router.request_detail(10 ** 9), None)

            client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            client.request("GET", "/request/99999999")
            missing = client.getresponse()
            self.assertEqual(missing.status, 404)
            missing.read()
            client.close()

            client = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            client.request("POST", "/config", body=b'{"tokens":4096}', headers={"Content-Type": "application/json"})
            configured = json.loads(client.getresponse().read())
            self.assertEqual(configured["tokens"], 4096)
            self.assertEqual(router.stats_snapshot()["token_limit"], 4096, "the dashboard budget applies without a restart")
            client.request("POST", "/config", body=b'{"tokens":1}', headers={"Content-Type": "application/json"})
            rejected = client.getresponse()
            self.assertEqual(rejected.status, 400)
            rejected.read()

            client.request("POST", "/config", body=b'{"summary_tokens":200}', headers={"Content-Type": "application/json"})
            configured = json.loads(client.getresponse().read())
            self.assertEqual(configured["summary_tokens"], 200)
            snapshot = router.stats_snapshot()
            self.assertEqual(snapshot["summary_tokens"], 200, "the global block output length applies without a restart")
            self.assertEqual(snapshot["summary_token_steps"], [50, 100, 200, 400])
            client.request("POST", "/config", body=b'{"summary_tokens":123}', headers={"Content-Type": "application/json"})
            bad = client.getresponse()
            self.assertEqual(bad.status, 400, "a length outside the fixed token menu is rejected")
            bad.read()
            client.close()
            client.close()
        finally:
            proxy.shutdown()
            proxy.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_config_valid_threshold_with_invalid_length_does_not_partially_update(self):
        """One POST carries both fields; validating the whole payload before applying keeps a rejected length
        from leaving the threshold moved."""
        real_budget, real_summary = router.BUDGET["tokens"], router.SUMMARY["tokens"]
        server = router.make_server("127.0.0.1", 0, "https://upstream.invalid")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            router.BUDGET["tokens"], router.SUMMARY["tokens"] = 64000, 100
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            client.request("POST", "/config", body=b'{"tokens":8192,"summary_tokens":123}', headers={"Content-Type": "application/json"})
            bad = client.getresponse()
            self.assertEqual(bad.status, 400)
            bad.read()
            self.assertEqual(router.BUDGET["tokens"], 64000, "a rejected length must not let the valid threshold apply")
            self.assertEqual(router.SUMMARY["tokens"], 100, "the current length is untouched by the rejected payload")
            client.request("POST", "/config", body=b'{"tokens":8192,"summary_tokens":200}', headers={"Content-Type": "application/json"})
            ok = client.getresponse()
            self.assertEqual(ok.status, 200)
            self.assertEqual(json.loads(ok.read()), {"tokens": 8192, "summary_tokens": 200})
            self.assertEqual(router.BUDGET["tokens"], 8192)
            self.assertEqual(router.SUMMARY["tokens"], 200)
            client.close()
        finally:
            server.shutdown()
            server.server_close()
            router.BUDGET["tokens"], router.SUMMARY["tokens"] = real_budget, real_summary

    def test_request_window_persists_and_rolls_over_the_last_100(self):
        for i in range(150):
            router.record_request({"at": 1000.0 + i, "route": "deepseek", "model": "deepseek-flash",
                                   "result": "compacted" if i % 3 == 0 else "passed", "status": 200, "ms": 10.0 + i,
                                   "raw_tokens": 1000, "sent_tokens": 400, "detail": "note"})
        recent = router.recent_requests(router.WINDOW)
        self.assertEqual(len(recent), router.WINDOW)
        self.assertEqual(recent[0]["at"], 1149.0, "newest first")
        self.assertEqual(recent[-1]["at"], 1050.0)
        window = router.stats_snapshot()["window"]
        self.assertEqual(window["size"], 100)
        self.assertEqual(window["saved_tokens"], 100 * 600)
        self.assertEqual(window["saved_percent"], 60.0)
        self.assertGreater(window["p95_ms"], window["p50_ms"])
        self.assertEqual(window["results"]["compacted"] + window["results"]["passed"], 100)
        # read straight from the file: the window is on disk, not an in-memory ring
        conn = router.db_connect()
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 150)
        finally:
            conn.close()
        keep = router.REQUESTS_KEEP
        router.REQUESTS_KEEP = 50
        try:
            router.record_request({"at": 2000.0, "route": "local", "result": "passed"})
            conn = router.db_connect()
            try:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 51, "retention prunes old rows")
            finally:
                conn.close()
        finally:
            router.REQUESTS_KEEP = keep
    def test_upstream_cost_and_net_saving_are_recorded(self):
        usage = router.usage_from_tail(b'data: {"usage":{"prompt_tokens":1000,"prompt_cache_hit_tokens":400,"completion_tokens":20}}\n\ndata: [DONE]\n\n')
        self.assertEqual(router.billed_tokens(usage), (600, 400, 20))
        self.assertEqual(router.billed_tokens({"input_tokens": 100, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 30}}), (70, 30, 7))
        self.assertEqual(router.usage_from_tail(b"no usage here"), {})
        self.assertIsNone(router.build_cost("unknown-model", 10, {}, None))
        original = router.model_rates
        router.model_rates = lambda model: {"input": 0.3, "cacheRead": 0.006, "output": 1.2}
        try:
            cost = router.build_cost("deepseek-flash", 100_000, usage, {"in": 500, "out": 40, "usd": 0.0002, "calls": 1})
        finally:
            router.model_rates = original
        self.assertAlmostEqual(cost["sent_usd"], round((600 * 0.3 + 400 * 0.006 + 20 * 1.2) / 1e6, 6))
        self.assertEqual(cost["raw_cached"], 40_000, "raw baseline keeps the call's observed 40 % cache share")
        raw_usd = round((60_000 * 0.3 + 40_000 * 0.006 + 20 * 1.2) / 1e6, 6)
        self.assertAlmostEqual(cost["raw_usd"], raw_usd)
        self.assertAlmostEqual(cost["net_usd"], round(raw_usd - (600 * 0.3 + 400 * 0.006 + 20 * 1.2) / 1e6 - 0.0002, 6))
        router.record_request({"at": 5.0, "route": "deepseek", "model": "deepseek-flash", "result": "compacted",
                               "raw_tokens": 100_000, "sent_tokens": 20_000, "cost": cost})
        detail = router.request_detail(router.recent_requests(1)[0]["id"])
        self.assertEqual(detail["cost"]["net_usd"], cost["net_usd"])

    def test_reprice_moves_stored_rows_onto_the_cached_baseline(self):
        original = router.model_rates
        router.model_rates = lambda model: {"input": 0.3, "cacheRead": 0.006, "output": 1.2}
        try:
            router.record_request({"at": 7.0, "route": "deepseek", "model": "deepseek-flash", "result": "compacted",
                                   "raw_tokens": 100_000, "sent_tokens": 1_000,
                                   "cost": {"model": "deepseek-flash", "in": 600, "cached": 400, "out": 20,
                                            "condenser_calls": 0, "condenser_in": 0, "condenser_out": 0,
                                            "condenser_usd": 0.0, "raw_usd": 0.03, "sent_usd": 0.000206,
                                            "net_usd": 0.029794}})
            self.assertEqual(router.reprice_costs(), 1, "one all-input row repriced")
            self.assertEqual(router.reprice_costs(), 0, "idempotent once raw_cached is stored")
            detail = router.request_detail(router.recent_requests(1)[0]["id"])
        finally:
            router.model_rates = original
        self.assertEqual(detail["cost"]["raw_cached"], 40_000)
        self.assertAlmostEqual(detail["cost"]["raw_usd"], round((60_000 * 0.3 + 40_000 * 0.006 + 20 * 1.2) / 1e6, 6))
        self.assertAlmostEqual(detail["cost"]["net_usd"], round(detail["cost"]["raw_usd"] - detail["cost"]["sent_usd"], 6))

    def test_global_series_and_cost_backfill(self):
        now = time.time()
        router.record_request({"at": now - 60, "route": "deepseek", "model": "deepseek-flash", "result": "compacted",
                               "raw_tokens": 100_000, "sent_tokens": 20_000,
                               "cost": {"raw_usd": 0.03, "sent_usd": 0.006, "net_usd": 0.024, "net_percent": 80.0}})
        router.record_request({"at": now - 30, "route": "deepseek", "model": "deepseek-flash", "result": "compacted",
                               "raw_tokens": 50_000, "sent_tokens": 10_000})
        router.record_request({"at": now - 10, "route": "deepseek", "model": "deepseek-flash", "result": "passed",
                               "raw_tokens": 1_000, "sent_tokens": 1_000})
        global_before = router.global_stats()
        self.assertEqual(global_before["size"], 3)
        self.assertEqual(global_before["saved_tokens"], 120_000)
        self.assertEqual(global_before["saved_percent"], 79.5)
        self.assertAlmostEqual(global_before["net_usd"], 0.024)
        series = router.reclaimed_series(24)
        self.assertEqual(sum(p["saved_tokens"] for p in series), 120_000)
        self.assertEqual(sum(p["count"] for p in series), 3)
        original = router.model_rates
        router.model_rates = lambda model: {"input": 0.3, "cacheRead": 0.006, "output": 1.2}
        try:
            self.assertEqual(router.backfill_costs(), 2, "only the cost-less rows are filled")
            self.assertEqual(router.backfill_costs(), 0, "idempotent")
        finally:
            router.model_rates = original
        global_after = router.global_stats()
        self.assertAlmostEqual(global_after["net_usd"], 0.036)
        detail = router.request_detail(router.recent_requests(3)[1]["id"])
        self.assertTrue(detail["cost"]["estimated"])

    def test_compaction_plan_records_each_removed_block(self):
        self.stub_condenser()
        body = {"input": history_blocks(6) + [{"role": "user", "content": [{"type": "input_text", "text": "next"}]}]}
        report: dict = {}
        _output, changed = router.rewrite_request(json.dumps(body).encode(), token_limit=1000, report=report)
        self.assertTrue(changed)
        plan = json.loads(report["plan"])
        completed = router.split_blocks(body["input"])[:-1]
        self.assertEqual([b["id"] for b in plan["blocks"]], [router.block_id(b) for b in completed], "every completed block in order")
        self.assertTrue(all(b["chars"] > 0 for b in plan["blocks"]))
        self.assertTrue(all(b["tokens"] > 0 for b in plan["blocks"]), "plans are token-first as well")
        kept = [b for b in plan["blocks"] if not b["dropped"]]
        self.assertTrue(kept, "the newest block should have been summarized")
        self.assertTrue(all(b["summary"] and b["summary_chars"] > 0 and b["summary_tokens"] > 0 and b["preview"] for b in kept))
        self.assertEqual(plan["kept"], len(kept))
        # the plan is what the row stores, so it must survive the DB round trip
        router.record_request({"at": 1.0, "route": "deepseek", "model": "deepseek-flash", "result": "compacted", "raw_tokens": 900, "sent_tokens": 300, "plan": report["plan"]})
        detail = router.request_detail(router.recent_requests(1)[0]["id"])
        self.assertEqual(detail["plan"], plan)

    def test_chat_completions_compaction_records_a_plan_too(self):
        self.stub_condenser()
        messages = [{"role": "system", "content": "system"}]
        for index in range(6):
            messages.append({"role": "user", "content": f"old {index} " + "x" * 500})
            messages.append({"role": "assistant", "content": f"answer {index} " + "y" * 500})
        messages.append({"role": "user", "content": "current"})
        report: dict = {}
        _output, changed = router.rewrite_request(json.dumps({"messages": messages}).encode(), token_limit=1000, report=report)
        self.assertTrue(changed)
        plan = json.loads(report["plan"])
        prefix, blocks = router.split_message_blocks(messages)
        self.assertEqual(len(prefix), 1)
        self.assertEqual([b["id"] for b in plan["blocks"]], [router.block_id(b) for b in blocks[:-1]])

    def test_dashboard_is_the_built_frontend_bundle(self):
        """The dashboard is the Vite build in frontend/dist, not an inline string: the built index.html
        exists and every hashed asset it references resolves under dist/assets."""
        index = Path(router.FRONTEND_DIST) / "index.html"
        self.assertTrue(index.is_file(), "frontend/dist/index.html must be built")
        html = index.read_text()
        self.assertIn('id="root"', html)
        assets = re.findall(r'(?:src|href)="/dashboard/assets/([A-Za-z0-9._-]+)"', html)
        self.assertTrue(assets, "built index.html must reference hashed /dashboard/assets")
        for name in assets:
            self.assertTrue((Path(router.FRONTEND_DIST) / "assets" / name).is_file(), name)

    def test_dashboard_bundle_is_valid_javascript(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        bundles = sorted((Path(router.FRONTEND_DIST) / "assets").glob("*.js"))
        self.assertTrue(bundles, "frontend/dist/assets must contain the built bundle")
        result = subprocess.run([node, "--check", str(bundles[0])], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_row_age_is_measured_against_the_server_clock(self):
        """Ages compare `at` (stamped by this process) to the server's `now` through the clock skew kept in
        frontend/src/format.ts, so a browser clock that is thirty minutes off cannot shift every row's age
        or the 24 h window."""
        self.assertLess(abs(router.stats_snapshot()["now"] - time.time()), 5)
        source = (Path(router.FRONTEND_DIST).parent / "src" / "format.ts").read_text()
        self.assertIn("clock.skew = serverNow - Date.now() / 1000", source)
        self.assertIn("Math.max(0, serverNow() - at)", source)
        self.assertNotIn("Math.max(0, Date.now() / 1000 - at)", source)
        bundle = sorted((Path(router.FRONTEND_DIST) / "assets").glob("*.js"))[0].read_text()
        self.assertIn("skew", bundle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
