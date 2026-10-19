"""Offline regression tests; no real inference, credentials, or provider charges."""

import asyncio
import gzip
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import zstandard
from fastapi.testclient import TestClient

from backend import main, utils
from backend import db as db_module
from backend.db import Database


class StorageTests(unittest.TestCase):
    def test_simple_schema_and_restart_persistence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.db"
            db = Database(path)
            db.save_settings({"tokens": 12345})
            db.cache_put("block-model-4096", "note", utils.digest("note"))
            rid = db.start_request("POST /r/deepseek/chat/completions")
            reopened = Database(path)
            self.assertEqual(reopened.health(), "ok")
            self.assertEqual(reopened.settings(utils.DEFAULTS)["tokens"], 12345)
            self.assertEqual(reopened.cache_get("block-model-4096"), "note")
            self.assertEqual(reopened.detail(rid)["status"], 503)
            names = {
                r["name"]
                for r in reopened.rows(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertEqual(
                names - {"sqlite_sequence"}, {"requests", "cache", "settings"}
            )
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_metrics_windows_are_covering_and_health_is_cheap(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "metrics.db")
            now = int(time.time() * 1000)
            with db.connect() as conn:
                conn.executemany(
                    """INSERT INTO requests(created_at,route,status,changed,before_tokens,
                    after_tokens,duration_ms,cache_hits,original,compressed) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    [
                        (now - 10000, "/a", 200, 1, 100, 60, 250, 1, "payload", "note"),
                        (now - 120000, "/b", 500, 0, 20, 20, 50, 0, "payload", "payload"),
                        (now - 2 * 86400000, "/c", 200, 0, 10, 8, 10, 0, "payload", "note"),
                    ],
                )
            self.assertEqual(db.health(), "ok")
            self.assertEqual(db.quick_check(), "ok")
            stats = db.stats("24h")
            self.assertEqual(stats["windows"]["minute"]["count"], 1)
            self.assertEqual(stats["windows"]["hour"]["errors"], 1)
            self.assertEqual(stats["windows"]["day"]["in"], 120)
            self.assertEqual(stats["windows"]["all"]["count"], 3)
            self.assertEqual(stats["windows"]["all"]["span_ms"], 2 * 86400000 - 10000)
            self.assertEqual(len(db.stats("7d")["timeline"]), 2)
            self.assertEqual(len(db.stats("30d")["timeline"]), 2)
            with db.connect() as conn:
                plan = conn.execute(
                    "EXPLAIN QUERY PLAN SELECT count(*),sum(before_tokens) FROM requests INDEXED BY requests_metrics WHERE created_at>=?",
                    (now - 86400000,),
                ).fetchall()
            self.assertTrue(any("USING COVERING INDEX requests_metrics" in row["detail"] for row in plan), plan)


class CompressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        utils._SKIP_UNTIL.clear()
        self.folder = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.folder.name) / "test.db")
        self.config = {**utils.DEFAULTS, "tokens": 1024}
        self.body = {
            "model": "test",
            "messages": [
                {"role": "system", "content": "fixed rules"},
                {"role": "user", "content": "original user query"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path":"x"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "c1", "content": "evidence " * 1200},
                {"role": "user", "content": "previous"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "live"},
            ],
        }

    async def asyncTearDown(self):
        self.folder.cleanup()

    async def test_turns_cache_and_protected_tool_links(self):
        call = AsyncMock(return_value="A factual note with evidence")
        with patch.object(utils, "condenser", call):
            body, changes, hits = await utils.compress_body(
                self.body, self.config, self.db, None
            )
            self.assertEqual(body["messages"][:2], self.body["messages"][:2])
            self.assertEqual(
                body["messages"][2],
                {"role": "assistant", "content": "A factual note with evidence"},
            )
            self.assertEqual(body["messages"][-3:], self.body["messages"][-3:])
            self.assertNotIn("original user query", call.call_args.args[1])
            self.assertEqual(changes[0]["before"][2]["tool_call_id"], "c1")
            self.assertIn("tool_call:", call.call_args.args[1])
            self.assertIn("tool_output:", call.call_args.args[1])
            body2, changes2, hits = await utils.compress_body(
                self.body, self.config, self.db, None
            )
            self.assertEqual(body, body2)
            self.assertEqual(call.await_count, 1)
            self.assertEqual(hits, 1)
            self.assertTrue(changes2[0]["cached"])
            await utils.compress_body(
                self.body, {**self.config, "maxTokens": 2048}, self.db, None
            )
            self.assertEqual(call.await_count, 2)

    async def test_query_is_verbatim_including_whitespace_and_metadata(self):
        query = {
            "role": "user",
            "name": "operator",
            "content": '  Exact query:\n"quoted" 😀\n\tkeep this  ',
        }
        self.body["messages"][1] = query
        before = utils.canonical(self.body)
        with patch.object(
            utils, "condenser", AsyncMock(return_value="one assistant note")
        ) as call:
            result, changes, _ = await utils.compress_body(
                self.body, self.config, self.db, None
            )
        self.assertEqual(result["messages"][1], query)
        self.assertEqual(
            result["messages"][2],
            {"role": "assistant", "content": "one assistant note"},
        )
        self.assertEqual(len(changes[0]["after"]), 2)
        self.assertNotIn(query["content"], call.call_args.args[1])
        self.assertEqual(utils.canonical(self.body), before)

    async def test_cache_keys_cover_activity_only_and_ignore_old_turn_cache(self):
        block = self.body["messages"][1:4]
        legacy_key = (
            f"{utils.digest(block)}-{self.config['model']}-{self.config['maxTokens']}"
        )
        self.db.cache_put(
            legacy_key, "old summary containing the query", utils.digest("old note")
        )
        with patch.object(
            utils, "condenser", AsyncMock(return_value="activity note")
        ) as call:
            first, _, _ = await utils.compress_body(
                self.body, self.config, self.db, None
            )
            self.body["messages"][1]["content"] = "a different exact query"
            second, changes, hits = await utils.compress_body(
                self.body, self.config, self.db, None
            )
        self.assertEqual(call.await_count, 1)
        self.assertEqual(first["messages"][1]["content"], "original user query")
        self.assertEqual(second["messages"][1]["content"], "a different exact query")
        self.assertEqual(second["messages"][2]["content"], "activity note")
        self.assertTrue(changes[0]["cached"])
        self.assertEqual(hits, 1)
        key = f"{utils.digest(block[1:])}-{self.config['model']}-{self.config['maxTokens']}"
        self.assertEqual(self.db.cache_get(key), "activity note")
        self.assertEqual(
            self.db.cache_get(legacy_key), "old summary containing the query"
        )

    async def test_existing_assistant_note_is_not_recondensed(self):
        with patch.object(
            utils, "condenser", AsyncMock(return_value="note " * 2000)
        ) as call:
            first, _, _ = await utils.compress_body(
                self.body, self.config, self.db, None
            )
            second, changes, hits = await utils.compress_body(
                first, self.config, self.db, None
            )
        self.assertEqual(call.await_count, 1)
        self.assertEqual(second, first)
        self.assertEqual(changes, [])
        self.assertEqual(hits, 1)

    async def test_verbatim_query_overflow_still_condenses_only_activity(self):
        query = "large exact query " * 1000
        self.body["messages"][1]["content"] = query
        with patch.object(
            utils, "condenser", AsyncMock(return_value="activity note")
        ) as call:
            result, changes, _ = await utils.compress_body(
                self.body, self.config, self.db, None
            )
        self.assertEqual(result["messages"][1], self.body["messages"][1])
        self.assertEqual(result["messages"][2]["content"], "activity note")
        self.assertEqual(result["messages"][-3:], self.body["messages"][-3:])
        self.assertEqual(len(changes), 1)
        self.assertLess(utils.tokens(result), utils.tokens(self.body))
        call.assert_awaited_once()
        self.assertNotIn(query, call.call_args.args[1])

    async def test_protected_overflow_still_condenses_completed_activity(self):
        for field in ("messages", "input"):
            with self.subTest(field=field):
                query = {"role": "user", "content": "  newest exact query 😀  "}
                if field == "input":
                    activity = [
                        {
                            "type": "function_call",
                            "call_id": "live",
                            "name": "read",
                            "arguments": "{}",
                        },
                        {
                            "type": "function_call_output",
                            "call_id": "live",
                            "output": "live evidence " * 1000,
                        },
                    ]
                else:
                    activity = [
                        {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "live",
                                    "type": "function",
                                    "function": {"name": "read", "arguments": "{}"},
                                }
                            ],
                        },
                        {
                            "role": "tool",
                            "tool_call_id": "live",
                            "content": "live evidence " * 1000,
                        },
                    ]
                body = {
                    field: [
                        {"role": "system", "content": "fixed rules"},
                        {"role": "user", "content": "older exact query"},
                        {
                            "role": "assistant",
                            "content": f"{field} old evidence " * 2000,
                        },
                        query,
                        *activity,
                    ]
                }
                before = utils.canonical(body)
                with patch.object(
                    utils, "condenser", AsyncMock(return_value="older activity note")
                ) as call:
                    result, changes, _ = await utils.compress_body(
                        body, {**self.config, "keepTurns": 1}, self.db, None
                    )
                self.assertEqual(result[field][-3:], [query, *activity])
                self.assertEqual(result[field][:2], body[field][:2])
                self.assertEqual(
                    result[field][2],
                    {"role": "assistant", "content": "older activity note"},
                )
                self.assertEqual(len(changes), 1)
                self.assertLess(utils.tokens(result), utils.tokens(body))
                self.assertGreater(utils.tokens(result), self.config["tokens"])
                self.assertEqual(utils.canonical(body), before)
                call.assert_awaited_once()
                self.assertNotIn("live evidence", call.call_args.args[1])

    async def test_indivisible_image_overflow_does_not_disable_other_blocks(self):
        image_block = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "image query " * 1000},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,abcd"},
                    },
                ],
            },
            {"role": "assistant", "content": "image activity"},
        ]
        self.body["messages"][1:1] = image_block
        with patch.object(
            utils, "condenser", AsyncMock(return_value="older activity note")
        ) as call:
            result, _, _ = await utils.compress_body(
                self.body, self.config, self.db, None
            )
        self.assertEqual(result["messages"][1:3], image_block)
        self.assertLess(utils.tokens(result), utils.tokens(self.body))
        call.assert_awaited_once()

    async def test_consecutive_queries_join_below_threshold_without_inference(self):
        body = {
            "messages": [
                {"role": "system", "content": "fixed"},
                {"role": "user", "content": "  first\n"},
                {"role": "user", "content": 'second "quoted" 😀'},
                {"role": "user", "content": "\tthird  "},
                {"role": "assistant", "content": "answer"},
            ]
        }
        original = utils.canonical(body)
        with patch.object(utils, "condenser", AsyncMock()) as call:
            result, changes, _ = await utils.compress_body(
                body, self.config, self.db, None
            )
        self.assertEqual(
            result["messages"],
            [
                body["messages"][0],
                {"role": "user", "content": '  first\n\nsecond "quoted" 😀\n\tthird  '},
                body["messages"][-1],
            ],
        )
        self.assertEqual([c["kind"] for c in changes], ["merge_users", "merge_users"])
        self.assertEqual(utils.canonical(body), original)
        call.assert_not_awaited()

    async def test_consecutive_responses_queries_keep_content_parts_and_images(self):
        parts = [
            {"type": "input_text", "text": "  first 😀  "},
            {"type": "input_image", "image_url": "data:image/png;base64,abcd"},
        ]
        body = {
            "input": [
                {"role": "user", "content": parts},
                {"role": "user", "content": "second\n"},
                {"role": "assistant", "content": "answer"},
            ]
        }
        result, changes, _ = await utils.compress_body(body, self.config, self.db, None)
        self.assertEqual(
            result["input"][0]["content"],
            parts
            + [
                {"type": "input_text", "text": "\n"},
                {"type": "input_text", "text": "second\n"},
            ],
        )
        self.assertEqual(result["input"][1], body["input"][2])
        self.assertEqual(len(changes), 1)

    async def test_tool_result_user_items_are_activity_not_queries(self):
        items = [
            {"role": "user", "content": "query"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "c", "content": "evidence"}
                ],
            },
            {"role": "user", "content": "next query"},
        ]
        joined, changes = utils.concatenate_users(items, "messages")
        self.assertEqual(joined, items)
        self.assertEqual(changes, [])
        _, groups = utils.turns(joined)
        self.assertEqual(groups, [items[:2], items[2:]])

    async def test_consecutive_queries_use_one_turn_then_summarize_activity(self):
        self.body["messages"].insert(
            2, {"role": "user", "content": "follow-up exact query"}
        )
        with patch.object(
            utils, "condenser", AsyncMock(return_value="single assistant note")
        ) as call:
            result, changes, _ = await utils.compress_body(
                self.body, self.config, self.db, None
            )
        self.assertEqual(
            result["messages"][1],
            {"role": "user", "content": "original user query\nfollow-up exact query"},
        )
        self.assertEqual(
            result["messages"][2],
            {"role": "assistant", "content": "single assistant note"},
        )
        self.assertEqual([c["kind"] for c in changes], ["merge_users", "block"])
        self.assertEqual(call.await_count, 1)
        self.assertNotIn("original user query", call.call_args.args[1])
        self.assertNotIn("follow-up exact query", call.call_args.args[1])

    async def test_user_only_group_never_gets_an_invented_assistant(self):
        body = {
            "messages": [
                {"role": "user", "content": "a" * 4000},
                {"role": "user", "content": "b" * 4000},
            ]
        }
        with patch.object(utils, "condenser", AsyncMock()) as call:
            result, changes, _ = await utils.compress_body(
                body, self.config, self.db, None
            )
        self.assertEqual(
            result["messages"],
            [{"role": "user", "content": "a" * 4000 + "\n" + "b" * 4000}],
        )
        self.assertEqual(changes[0]["kind"], "merge_users")
        call.assert_not_awaited()

    async def test_failure_forwards_block_verbatim_without_caching(self):
        with patch.object(
            utils, "condenser", AsyncMock(side_effect=RuntimeError("offline"))
        ) as call:
            result, changes, hits = await utils.compress_body(
                self.body, self.config, self.db, None
            )
            self.assertEqual(result, self.body)
            self.assertEqual(changes, [])
            self.assertEqual(hits, 0)
            self.assertEqual(call.await_count, 3)
            self.assertEqual(self.db.rows("SELECT count(*) n FROM cache")[0]["n"], 0)
            # The outage skips the condenser instead of retrying it per turn.
            again, changes, _ = await utils.compress_body(
                self.body, self.config, self.db, None
            )
        self.assertEqual(again, self.body)
        self.assertEqual(changes, [])
        self.assertEqual(call.await_count, 3)

    async def test_summary_hint_and_local_wire_format(self):
        received = []

        def fake(request):
            received.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "choices": [{"message": {"content": "<think>hidden</think>note"}}]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as client:
            result = await utils.condenser(client, "x" * 3500, self.config)
        self.assertEqual(result, "note")
        self.assertIn("approximately 200 tokens", received[0]["messages"][0]["content"])
        self.assertEqual(received[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual(received[0]["max_tokens"], self.config["maxTokens"])
        self.assertEqual(utils.tokens("😀" * 3500), 1000)

    async def test_timeout_covers_each_attempt(self):
        async def slow(*args):
            await asyncio.Event().wait()

        with (
            patch.object(utils, "condenser", slow),
            patch.object(utils, "ATTEMPT_TIMEOUT", 0.01),
        ):
            self.assertEqual(
                await utils.ctx_man(
                    self.body["messages"][2:4], self.config, self.db, None
                ),
                (None, False),
            )

    async def test_overflow_never_drops_or_clips(self):
        body = {**self.body, "instructions": "fixed " * 3000}
        with patch.object(
            utils, "condenser", AsyncMock(return_value="activity note")
        ) as call:
            result, changes, _ = await utils.compress_body(
                body, self.config, self.db, None
            )
            self.assertEqual(result["instructions"], body["instructions"])
            self.assertEqual(result["messages"][:2], body["messages"][:2])
            self.assertEqual(result["messages"][-3:], body["messages"][-3:])
            self.assertLess(utils.tokens(result), utils.tokens(body))
            self.assertEqual(len(changes), 1)
            call.assert_awaited_once()

    async def test_nonreducing_summary_keeps_original_block_and_reuses_cache(self):
        note = "large note " * 3000
        with patch.object(utils, "condenser", AsyncMock(return_value=note)) as call:
            first, changes, _ = await utils.compress_body(
                self.body, self.config, self.db, None
            )
            second, changes2, hits = await utils.compress_body(
                self.body, self.config, self.db, None
            )
        self.assertEqual(first, self.body)
        self.assertEqual(second, self.body)
        self.assertEqual(changes, [])
        self.assertEqual(changes2, [])
        self.assertEqual(hits, 1)
        call.assert_awaited_once()
        self.assertEqual(self.db.rows("SELECT output FROM cache")[0]["output"], note)

    async def test_responses_and_opaque_reasoning(self):
        body = {
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "query"}]},
                {
                    "type": "reasoning",
                    "encrypted_content": "DO NOT CONDENSE OPAQUE",
                    "summary": [],
                },
                {
                    "type": "function_call",
                    "call_id": "c1",
                    "name": "read",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "c1",
                    "output": "data " * 3000,
                },
                {"role": "user", "content": "previous"},
                {"role": "assistant", "content": "previous answer"},
                {"role": "user", "content": "live"},
            ]
        }
        with patch.object(utils, "condenser", AsyncMock(return_value="note")) as call:
            result, _, _ = await utils.compress_body(body, self.config, self.db, None)
            self.assertEqual(result["input"][0], body["input"][0])
            self.assertEqual(
                result["input"][1], {"role": "assistant", "content": "note"}
            )
            self.assertEqual(result["input"][-3:], body["input"][-3:])
            self.assertNotIn("user:\nquery", call.call_args.args[1])
            self.assertNotIn("DO NOT CONDENSE OPAQUE", call.call_args.args[1])

    async def test_parallel_fanout_and_image_preservation(self):
        count = 0
        ready = asyncio.Event()

        async def simultaneous(*args):
            nonlocal count
            count += 1
            if count == 3:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=1)
            return "note"

        body = {
            "messages": [
                item
                for i in range(5)
                for item in (
                    {"role": "user", "content": f"query {i}"},
                    {"role": "assistant", "content": "x" * (5000 if i < 3 else 10)},
                )
            ]
        }
        with patch.object(utils, "condenser", simultaneous):
            result, changes, _ = await utils.compress_body(
                body, self.config, self.db, None
            )
            self.assertEqual(len(changes), 3)
        image = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abcd"},
        }
        body["messages"][0]["content"] = [{"type": "text", "text": "x" * 5000}, image]
        with patch.object(utils, "condenser", AsyncMock(return_value="note")):
            result, _, _ = await utils.compress_body(body, self.config, self.db, None)
            self.assertEqual(result["messages"][0], body["messages"][0])


class ProxyTests(unittest.TestCase):
    def setUp(self):
        utils._SKIP_UNTIL.clear()
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "test.db"
        self.patch_db = patch.object(main, "DB_PATH", str(self.path))
        self.patch_db.start()
        self.client = TestClient(main.app, base_url="http://127.0.0.1:4141")
        self.client.__enter__()
        self.real_client = main.app.state.client
        self.received = []

        def upstream(request):
            self.received.append(request)
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(b'data: {"ok":true}\n\ndata: [DONE]\n\n'),
            )

        main.app.state.client = httpx.AsyncClient(
            transport=httpx.MockTransport(upstream)
        )

    def tearDown(self):
        self.client.__exit__(None, None, None)
        asyncio.run(self.real_client.aclose())
        self.patch_db.stop()
        self.folder.cleanup()

    def test_api_history_live_streaming_and_empty_body(self):
        response = self.client.post(
            "/r/deepseek/chat/completions",
            json={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("[DONE]", response.text)
        self.assertEqual(response.headers["x-context-router"], "pass")
        rows = self.client.get("/api/history").json()["items"]
        self.assertEqual(rows[0]["status"], 200)
        detail = self.client.get(f"/api/history/{rows[0]['id']}").json()
        self.assertEqual(detail["original"]["messages"][0]["content"], "hi")
        self.assertEqual(detail["compressed"], detail["original"])
        self.assertEqual(self.client.post("/r/deepseek/logout").status_code, 200)
        self.assertEqual(self.received[-1].content, b"")
        self.assertEqual(self.client.get("/r/deepseek/models?x=1").status_code, 200)
        self.assertEqual(self.received[-1].url.query, b"x=1")
        self.assertEqual(self.client.get("/health").json()["database"], "ok")

    def test_proxy_keeps_queries_and_one_assistant_note_in_both_wire_formats(self):
        main.app.state.db.save_settings({**utils.DEFAULTS, "tokens": 1024})
        for field, route in (
            ("messages", "/r/deepseek/chat/completions"),
            ("input", "/r/openai/responses"),
        ):
            for encoding in ("identity", "gzip", "zstd"):
                with self.subTest(field=field, encoding=encoding):
                    body = {
                        field: [
                            {"role": "user", "content": "  exact first query\n"},
                            {"role": "user", "content": "second exact query 😀  "},
                            {
                                "role": "assistant",
                                "content": "assistant evidence " * 1000,
                            },
                            {"role": "user", "content": "previous"},
                            {"role": "assistant", "content": "previous answer"},
                            {"role": "user", "content": "live"},
                        ]
                    }
                    raw = json.dumps(body).encode()
                    if encoding == "gzip":
                        raw = gzip.compress(raw)
                    elif encoding == "zstd":
                        raw = zstandard.ZstdCompressor().compress(raw)
                    with patch.object(
                        utils,
                        "condenser",
                        AsyncMock(return_value="one assistant summary"),
                    ):
                        response = self.client.post(
                            route,
                            content=raw,
                            headers={
                                "content-type": "application/json",
                                "content-encoding": encoding,
                            },
                        )
                    self.assertEqual(response.status_code, 200)
                    received = self.received[-1].content
                    if encoding == "gzip":
                        received = gzip.decompress(received)
                    elif encoding == "zstd":
                        received = zstandard.ZstdDecompressor().decompress(received)
                    forwarded = json.loads(received)
                    self.assertEqual(
                        forwarded[field][:2],
                        [
                            {
                                "role": "user",
                                "content": "  exact first query\n\nsecond exact query 😀  ",
                            },
                            {"role": "assistant", "content": "one assistant summary"},
                        ],
                    )
                    self.assertEqual(forwarded[field][-3:], body[field][-3:])
                    rid = self.client.get("/api/history?limit=1").json()["items"][0][
                        "id"
                    ]
                    detail = self.client.get(f"/api/history/{rid}").json()
                    self.assertEqual(detail["original"], body)
                    self.assertEqual(detail["compressed"], forwarded)
                    self.assertEqual(
                        [change["kind"] for change in detail["changes"]],
                        ["merge_users", "block"],
                    )

    def test_proxy_condenses_older_activity_when_protected_floor_overflows(self):
        main.app.state.db.save_settings(
            {**utils.DEFAULTS, "tokens": 1024, "keepTurns": 1}
        )
        for field, route in (
            ("messages", "/r/deepseek/chat/completions"),
            ("input", "/r/openai/responses"),
        ):
            with self.subTest(field=field):
                body = {
                    field: [
                        {"role": "user", "content": "  exact older query 😀  "},
                        {
                            "role": "assistant",
                            "content": f"{field} old evidence " * 2000,
                        },
                        {"role": "user", "content": "newest exact query"},
                        {"role": "assistant", "content": "protected evidence " * 1000},
                    ]
                }
                with patch.object(
                    utils, "condenser", AsyncMock(return_value="older activity note")
                ) as call:
                    response = self.client.post(route, json=body)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["x-context-router"], "compacted")
                forwarded = json.loads(self.received[-1].content)
                self.assertEqual(forwarded[field][0], body[field][0])
                self.assertEqual(forwarded[field][-2:], body[field][-2:])
                self.assertEqual(forwarded[field][1]["content"], "older activity note")
                self.assertLess(utils.tokens(forwarded), utils.tokens(body))
                call.assert_awaited_once()
                latest = self.client.get("/api/history?limit=1").json()["items"][0]
                self.assertLess(latest["after_tokens"], latest["before_tokens"])
                detail = self.client.get(f"/api/history/{latest['id']}").json()
                self.assertEqual(detail["original"], body)
                self.assertEqual(detail["compressed"], forwarded)
                self.assertEqual([c["kind"] for c in detail["changes"]], ["block"])

    def test_oauth_normalization_for_both_routes_and_api_key(self):
        body = {
            "model": "m",
            "input": [],
            "max_output_tokens": 99,
            "temperature": 1,
            "prompt_cache_retention": "24h",
            "prompt_cache_options": {},
        }
        for route in ("/r/openai/responses", "/codex/responses"):
            self.assertEqual(
                self.client.post(
                    route, json=body, headers={"Authorization": "Bearer oauth"}
                ).status_code,
                200,
            )
            value = json.loads(self.received[-1].content)
            self.assertNotIn("max_output_tokens", value)
            self.assertNotIn("temperature", value)
            self.client.post(
                route, json=body, headers={"Authorization": "Bearer sk-test"}
            )
            self.assertEqual(json.loads(self.received[-1].content), body)

    def test_encoded_bodies_and_request_limits(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "héllo"}]}
        raw = gzip.compress(json.dumps(body).encode())
        response = self.client.post(
            "/r/deepseek/chat/completions",
            content=raw,
            headers={"content-encoding": "gzip", "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.received[-1].content, raw)
        raw = gzip.compress(json.dumps({**body, "temperature": 1}).encode())
        self.client.post(
            "/r/openai/responses",
            content=raw,
            headers={"content-encoding": "gzip", "authorization": "Bearer oauth"},
        )
        self.assertNotIn(
            "temperature", json.loads(gzip.decompress(self.received[-1].content))
        )
        with patch.object(main, "MAX_BODY", 10):
            self.assertEqual(
                self.client.post(
                    "/r/deepseek/chat/completions", content=b"x" * 20
                ).status_code,
                400,
            )
        self.assertEqual(
            self.client.post(
                "/r/deepseek/chat/completions",
                content=b"bad",
                headers={"content-encoding": "gzip"},
            ).status_code,
            400,
        )

    def test_condenser_failure_forwards_original_and_succeeds(self):
        body = {
            "messages": [
                {"role": "user", "content": "query"},
                {"role": "assistant", "content": "x" * 20000},
                {"role": "user", "content": "prev"},
                {"role": "assistant", "content": "previous answer"},
                {"role": "user", "content": "live"},
                {"role": "assistant", "content": "protected evidence " * 1000},
            ]
        }
        main.app.state.db.save_settings({**utils.DEFAULTS, "tokens": 1024})
        with patch.object(utils, "condenser", AsyncMock(side_effect=RuntimeError())):
            response = self.client.post("/r/deepseek/chat/completions", json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["x-context-router"], "pass")
        self.assertEqual(json.loads(self.received[-1].content), body)
        latest = self.client.get("/api/history").json()["items"][0]
        self.assertEqual(latest["status"], 200)
        self.assertEqual(
            self.client.get(f"/api/history/{latest['id']}").json()["original"], body
        )
        self.assertEqual(self.client.get("/stats").json()["history"]["errors"], 0)

    def test_management_security_settings_and_pagination(self):
        self.assertEqual(
            self.client.get(
                "/api/history", headers={"Origin": "https://evil.example"}
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.put(
                "/api/settings", json={**utils.DEFAULTS, "tokens": -1}
            ).status_code,
            422,
        )
        config = {**utils.DEFAULTS, "tokens": 12345}
        self.assertEqual(self.client.put("/api/settings", json=config).status_code, 200)
        self.assertEqual(self.client.get("/api/settings").json()["settings"], config)
        partial = self.client.put("/api/settings", json={"keepTurns": 3}).json()[
            "settings"
        ]
        self.assertEqual(partial["tokens"], 12345)
        self.assertEqual(partial["keepTurns"], 3)
        for _ in range(3):
            self.client.get("/r/deepseek/models")
        page = self.client.get("/api/history?limit=2").json()
        self.assertEqual(len(page["items"]), 2)
        self.assertEqual(page["total"], 3)
        older = self.client.get(f"/api/history?limit=2&before={page['next']}").json()
        self.assertEqual(len(older["items"]), 1)
        self.assertEqual(self.client.get("/api/history?limit=0").status_code, 400)
        self.assertEqual(self.client.get("/dashboard/").status_code, 200)
        self.assertEqual(self.client.get("/api/history/999").status_code, 404)

    def test_health_deep_and_stats_ranges(self):
        with patch.object(main.app.state.db, "quick_check", return_value="ok") as check:
            self.assertEqual(self.client.get("/health").json()["database"], "ok")
            check.assert_not_called()
            self.assertEqual(
                self.client.get("/health?deep=true").json()["database"], "ok"
            )
            check.assert_called_once_with()
        self.assertIn("windows", self.client.get("/stats?range=7d").json())
        self.assertEqual(self.client.get("/stats?range=invalid").status_code, 400)

    def test_database_outage_is_503_not_bad_credentials(self):
        with patch.object(
            main.app.state.db,
            "health",
            side_effect=sqlite3.OperationalError("unavailable"),
        ):
            self.assertEqual(self.client.get("/health").status_code, 503)
        with patch.object(
            main.app.state.db,
            "start_request",
            side_effect=sqlite3.DatabaseError("unavailable"),
        ):
            self.assertEqual(
                self.client.post("/r/deepseek/chat/completions", json={}).status_code,
                503,
            )

    def test_antigravity_passthrough_route(self):
        payload = {
            "project": "test-proj",
            "model": "gemini-2.5-pro",
            "request": {
                "contents": [
                    {"role": "user", "parts": [{"text": "explain quantum computing"}]}
                ]
            },
        }
        response = self.client.post(
            "/r/antigravity/v1internal:streamGenerateContent?alt=sse",
            json=payload,
            headers={"Authorization": "Bearer ya29.test-oauth-token"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            str(self.received[-1].url),
            "https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse",
        )
        forwarded = json.loads(self.received[-1].content)
        self.assertEqual(forwarded, payload)
        self.assertIn(
            "ya29.test-oauth-token", self.received[-1].headers["authorization"]
        )


class DeltaAndZstdOptimizationTests(unittest.TestCase):
    def test_zstd_binary_blobs_and_no_raw_payloads(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "zstd.db")
            body = {
                "model": "deepseek-flash",
                "messages": [
                    {"role": "user", "content": "hello world"},
                    {"role": "assistant", "content": "greetings"},
                ],
            }
            rid = db.start_request("POST /r/deepseek/chat/completions")
            trace = {
                "model": "deepseek-flash",
                "status": 200,
                "changed": False,
                "before_tokens": 15,
                "after_tokens": 15,
                "duration_ms": 100,
                "cache_hits": 0,
                "error": None,
                "original": body,
                "compressed": body,
                "changes": [],
            }
            db.finish_request(rid, trace)

            # Check raw storage in SQLite
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT original, compressed, typeof(original), typeof(compressed) FROM requests WHERE id=?",
                    (rid,),
                ).fetchone()

            # original MUST be a binary blob starting with Zstandard magic bytes, NOT raw text
            self.assertEqual(row[2], "blob")
            self.assertTrue(row[0].startswith(db_module.ZSTD_MAGIC))
            # compressed MUST be NULL because changed == False (no duplicate storage)
            self.assertEqual(row[3], "null")
            self.assertIsNone(row[1])

            # detail() must transparently decompress and reconstruct identical payloads
            detail = db.detail(rid)
            self.assertEqual(detail["original"], body)
            self.assertEqual(detail["compressed"], body)
            self.assertFalse(detail["delta"]["changed"])
            self.assertEqual(detail["delta"]["summary"], "identical")

    def test_delta_calculation_and_condensed_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "delta.db")
            original_body = {
                "model": "deepseek-flash",
                "messages": [
                    {"role": "user", "content": "turn 1 query"},
                    {"role": "assistant", "content": "turn 1 response"},
                    {"role": "user", "content": "turn 2 query"},
                ],
                "max_output_tokens": 100,
            }
            compressed_body = {
                "model": "deepseek-flash",
                "messages": [
                    {"role": "user", "content": "turn 1 query"},
                    {"role": "assistant", "content": "summary note"},
                    {"role": "user", "content": "turn 2 query"},
                ],
            }
            changes = [
                {
                    "kind": "subscription",
                    "before": {"max_output_tokens": 100},
                    "after": None,
                },
                {"kind": "block", "before": 20, "after": 5},
            ]
            rid = db.start_request("POST /r/deepseek/chat/completions")
            trace = {
                "model": "deepseek-flash",
                "status": 200,
                "changed": True,
                "before_tokens": 50,
                "after_tokens": 25,
                "duration_ms": 150,
                "cache_hits": 1,
                "error": None,
                "original": original_body,
                "compressed": compressed_body,
                "changes": changes,
            }
            db.finish_request(rid, trace)

            detail = db.detail(rid)
            self.assertEqual(detail["original"], original_body)
            self.assertEqual(detail["compressed"], compressed_body)
            self.assertTrue(detail["delta"]["changed"])
            self.assertEqual(detail["delta"]["removed_keys"], ["max_output_tokens"])
            self.assertEqual(detail["delta"]["condensed_blocks"], 1)

    def test_in_memory_zstd_compression_for_data_older_than_one_day(self):
        store = db_module.InMemoryZstdStore(max_items=100)
        now_ms = int(time.time() * 1000)
        recent_ts = now_ms - 3600000  # 1 hour ago (< 1 day)
        old_ts = now_ms - 2 * db_module.ONE_DAY_MS  # 2 days ago (> 1 day)

        recent_data = {"key": "recent_value", "tokens": 123}
        old_data = {"key": "old_value", "tokens": 456}

        store.put("recent_item", recent_data, created_at_ms=recent_ts)
        store.put("old_item", old_data, created_at_ms=old_ts)

        # Recent item MUST NOT be compressed in memory
        self.assertFalse(store.is_compressed("recent_item"))
        # Old item MUST be compressed with Zstandard in memory
        self.assertTrue(store.is_compressed("old_item"))

        # Both MUST retrieve exact original data transparently
        self.assertEqual(store.get("recent_item"), recent_data)
        self.assertEqual(store.get("old_item"), old_data)

        # Aging simulation: item ages past 1 day
        store.put("aging_item", {"temp": "data"}, created_at_ms=recent_ts)
        self.assertFalse(store.is_compressed("aging_item"))
        # Artificially set timestamp to 2 days ago
        store._store["aging_item"] = (
            now_ms - 2 * db_module.ONE_DAY_MS,
            False,
            store._store["aging_item"][2],
        )
        compressed_count = store.compress_old_entries()
        self.assertEqual(compressed_count, 1)
        self.assertTrue(store.is_compressed("aging_item"))
        self.assertEqual(store.get("aging_item"), {"temp": "data"})

    def test_compact_old_database_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Database(Path(folder) / "compact.db")
            now = int(time.time() * 1000)
            old_time = now - 2 * db_module.ONE_DAY_MS

            # Insert legacy uncompressed raw text rows older than 1 day
            with db.connect() as conn:
                conn.execute(
                    """INSERT INTO requests(created_at,route,status,changed,before_tokens,
                    after_tokens,duration_ms,cache_hits,original,compressed) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        old_time,
                        "/test",
                        200,
                        0,
                        100,
                        100,
                        50,
                        0,
                        json.dumps({"msg": "legacy_raw"}),
                        json.dumps({"msg": "legacy_raw"}),
                    ),
                )
                conn.execute(
                    """INSERT INTO requests(created_at,route,status,changed,before_tokens,
                    after_tokens,duration_ms,cache_hits,original,compressed) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        now - 1000,
                        "/test_recent",
                        200,
                        0,
                        50,
                        50,
                        20,
                        0,
                        json.dumps({"msg": "recent_raw"}),
                        json.dumps({"msg": "recent_raw"}),
                    ),
                )

            res = db.compact_old_data(max_age_days=1)
            self.assertEqual(res["db_rows"], 1)

            with db.connect() as conn:
                old_row = conn.execute(
                    "SELECT typeof(original), typeof(compressed) FROM requests WHERE created_at=?",
                    (old_time,),
                ).fetchone()
                # Legacy text converted to zstd blob, redundant compressed nulled
                self.assertEqual(old_row[0], "blob")
                self.assertEqual(old_row[1], "null")


if __name__ == "__main__":
    unittest.main()
