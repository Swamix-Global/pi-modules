import fcntl
import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
import master_session as ms
import ctx_engine_utils as utils


class FakeHandler(BaseHTTPRequestHandler):
    calls = []
    fail_once = False

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        block = body["block"]
        type(self).calls.append(block)
        if type(self).fail_once and len(type(self).calls) == 2:
            type(self).fail_once = False
            self.send_response(502)
            self.end_headers()
            return
        digest = utils.block_id(block)
        answer = json.dumps({"id": digest, "note": "<compacted>\n- Task/Query: test\n- Actions Performed: done\n- Conclusion: done", "prompt_hash": "fake"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *_):
        pass


class MasterSessionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.sessions = self.root / "sessions"
        self.bucket = self.sessions / ms.encoded_project(str(self.project))
        self.bucket.mkdir(parents=True)
        self.output = self.root / "output"
        FakeHandler.calls = []
        FakeHandler.fail_once = False
        self.server = HTTPServer(("127.0.0.1", 0), FakeHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.router = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()
        self.temp.cleanup()

    def put_session(self, name, entries):
        path = self.bucket / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
        return path

    def test_raw_active_branch_compaction_checkpoint_resume_and_export(self):
        self.put_session("branch.jsonl", [
            {"type": "session", "id": "root", "cwd": str(self.project)},
            {"type": "message", "id": "u", "parentId": "root", "message": {"role": "user", "content": "active query"}},
            {"type": "message", "id": "other", "parentId": "u", "message": {"role": "assistant", "content": "inactive branch"}},
            {"type": "message", "id": "a", "parentId": "u", "message": {"role": "assistant", "content": "active answer"}},
        ])
        self.put_session("compact.jsonl", [
            {"type": "session", "id": "croot", "cwd": str(self.project)},
            {"type": "message", "id": "old", "parentId": "croot", "message": {"role": "user", "content": "old prompt"}},
            {"type": "message", "id": "kept", "parentId": "old", "message": {"role": "user", "content": "kept prompt"}},
            {"type": "compaction", "id": "cmp", "parentId": "kept", "summary": "native note", "firstKeptEntryId": "kept"},
            {"type": "message", "id": "last", "parentId": "cmp", "message": {"role": "assistant", "content": "kept answer"}},
        ])
        FakeHandler.fail_once = True
        with self.assertRaises(RuntimeError):
            ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
        raw = [json.loads(line) for line in (self.output / "master-raw.jsonl").read_text().splitlines()]
        self.assertEqual(sum(len(row["entries"]) for row in raw), 9)
        master_path = self.output / "master.json"
        saved = json.loads(master_path.read_text())
        self.assertEqual(len(saved["notes"]), 2)  # network note and native note were checkpointed before failure
        self.assertFalse((self.output / "mastersession.jsonl").exists())
        self.assertEqual(len(FakeHandler.calls), 2)
        ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
        self.assertEqual(len(FakeHandler.calls), 3)
        first_export = [json.loads(line) for line in (self.output / "mastersession.jsonl").read_text().splitlines()]
        calls = len(FakeHandler.calls)
        ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
        self.assertEqual(len(FakeHandler.calls), calls)
        notes = json.loads(master_path.read_text())["notes"]
        self.assertTrue(any("native note" in row["note"] for row in notes.values()))
        self.assertTrue(all("inactive branch" not in repr(call) and "old prompt" not in repr(call) for call in FakeHandler.calls))
        exported = [json.loads(line) for line in (self.output / "mastersession.jsonl").read_text().splitlines()]
        self.assertEqual(exported[0]["version"], 3)
        self.assertEqual(exported[0]["id"], first_export[0]["id"])
        self.assertEqual(exported[0]["timestamp"], first_export[0]["timestamp"])
        self.assertTrue(exported[0]["timestamp"].endswith("Z"))
        self.assertEqual(exported[1]["name"], "master")
        self.assertIsNone(exported[1]["parentId"])
        self.assertEqual(exported[2]["parentId"], "master-info")
        chain = "master-info"
        for row in exported[2:]:
            self.assertEqual(row["parentId"], chain)
            self.assertTrue(row["message"]["content"].startswith("<compacted>\n"))
            chain = row["id"]

    def test_limit_no_early_publish_and_resume(self):
        for i in range(3):
            self.put_session(f"{i}.jsonl", [
                {"type": "session", "id": f"r{i}", "cwd": str(self.project)},
                {"type": "message", "id": f"u{i}", "parentId": f"r{i}", "message": {"role": "user", "content": str(i)}},
            ])
        ms.process(str(self.project), self.bucket, self.output, self.router, True, 1)
        self.assertEqual(len(FakeHandler.calls), 1)
        self.assertFalse((self.output / "mastersession.jsonl").exists())
        ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
        self.assertEqual(len(FakeHandler.calls), 3)
        self.assertTrue((self.output / "mastersession.jsonl").exists())

    def test_oversize_chunks_include_tail_and_router_guard(self):
        tail = "Z" * 1_100_000
        block = [{"role": "user", "content": tail}]
        pieces = [tail[i:i + ms.CHUNK_CHARS] for i in range(0, len(tail), ms.CHUNK_CHARS)]
        synth = [[{"role": "user", "content": f"Source block {utils.block_id(block)}; chunk {i + 1}/{len(pieces)}\n{piece}"}] for i, piece in enumerate(pieces)]
        self.assertEqual("".join(part[0]["content"].split("\n", 1)[1] for part in synth), tail)
        with self.assertRaises(ValueError):
            ms.request_router("http://example.com", block)

    def test_encoded_project_uses_real_pi_bucket_name_and_nonempty_fixture_runs(self):
        expected = "--home-ubuntu-src-GigaTrader-v2--"
        self.assertEqual(ms.encoded_project("/home/ubuntu/src/GigaTrader-v2"), expected)
        fixture_bucket = self.sessions / expected
        fixture_bucket.mkdir()
        fixture_project = "/home/ubuntu/src/GigaTrader-v2"
        (fixture_bucket / "real.jsonl").write_text("\n".join(json.dumps(row) for row in [
            {"type": "session", "id": "root", "cwd": fixture_project},
            {"type": "message", "id": "u", "parentId": "root", "message": {"role": "user", "content": "real source"}},
        ]) + "\n", encoding="utf-8")
        already, pending = ms.process(fixture_project, fixture_bucket, self.output, self.router, True, None)
        self.assertEqual((already, pending), (1, 0))
        self.assertEqual(len(FakeHandler.calls), 1)
        self.assertIn("real source", repr(FakeHandler.calls[0]))
        self.assertTrue((self.output / "mastersession.jsonl").exists())

    def test_native_context_edits_replacement_omission_and_tool_result(self):
        entries = [
            {"type": "session", "id": "root"},
            {"type": "message", "id": "u", "parentId": "root", "message": {"role": "user", "content": "before"}},
            {"type": "message", "id": "tool", "parentId": "u", "message": {"role": "toolResult", "content": "result"}},
            {"type": "context_edit", "id": "edit1", "parentId": "tool", "targetId": "u", "replacement": {"content": "after"}},
            {"type": "context_edit", "id": "edit2", "parentId": "edit1", "targetId": "tool", "replacement": None},
        ]
        blocks = ms.blocks_for(entries)
        flattened = [message for block in blocks for message in block]
        self.assertTrue(any(message.get("role") == "user" and message.get("content") == "after" for message in flattened))
        self.assertFalse(any(message.get("content") == "result" for message in flattened))
        self.assertEqual(ms.entry_message({"type": "message", "message": {"role": "toolResult", "content": "x"}})["role"], "toolResult")
        array = ["part", {"type": "text", "text": "two"}]
        replaced = ms.blocks_for(entries[:2] + [dict(entries[3], id="arr", parentId="u", replacement={"content": array})])
        self.assertTrue(any(message.get("content") == array for block in replaced for message in block))
        tool_edit = entries[:3] + [{"type": "context_edit", "id": "edit-tool", "parentId": "tool", "targetId": "tool", "replacement": {"content": "short"}}]
        self.assertEqual(ms.blocks_for(tool_edit)[0][1]["content"], [{"type": "text", "text": "short"}])

    def test_branch_summary_does_not_cutoff_and_invalid_tree_fails_closed(self):
        entries = [
            {"type": "session", "id": "r"},
            {"type": "message", "id": "u", "parentId": "r", "message": {"role": "user", "content": "prior"}},
            {"type": "branch_summary", "id": "bs", "parentId": "u", "summary": "branch note"},
            {"type": "message", "id": "last", "parentId": "bs", "message": {"role": "user", "content": "later"}},
        ]
        self.assertIn("prior", repr(ms.blocks_for(entries)))
        for broken in (entries + [dict(entries[-1])], entries[:-1] + [dict(entries[-1], parentId="missing")]):
            with self.assertRaises(ValueError):
                ms.blocks_for(broken)
        with self.assertRaises(ValueError):
            ms.native_note({"type": "compaction", "summary": ""})

    def test_source_change_after_snapshot_rejected(self):
        path = self.put_session("changed.jsonl", [{"type": "session", "id": "r", "cwd": str(self.project)}])
        original = ms.read_stable
        def mutate_then_read(source, snapshot=None):
            source.write_text(source.read_text() + json.dumps({"type": "message", "id": "u", "parentId": "r", "message": {"role": "user", "content": "changed"}}) + "\n")
            return original(source, snapshot)
        with mock.patch.object(ms, "read_stable", side_effect=mutate_then_read):
            with self.assertRaisesRegex(ValueError, "changed since snapshot"):
                ms.materialize(str(self.project), self.bucket, self.output)

    def test_oversize_chunks_resume_without_losing_tail(self):
        self.put_session("large.jsonl", [
            {"type": "session", "id": "r", "cwd": str(self.project)},
            {"type": "message", "id": "u", "parentId": "r", "message": {"role": "user", "content": "ABCDEFGHIJKLMNO-TAIL"}},
        ])
        FakeHandler.fail_once = True
        with mock.patch.object(ms, "MAX_REQUEST", 1), mock.patch.object(ms, "CHUNK_CHARS", 10):
            with self.assertRaises(RuntimeError):
                ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
            saved_count = len(json.loads((self.output / "master.json").read_text())["notes"])
            self.assertEqual(saved_count, 1)
            ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
            self.assertEqual(len(FakeHandler.calls), 4)
            before = len(FakeHandler.calls)
            ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
            self.assertEqual(len(FakeHandler.calls), before)
        chunks = [call[0]["content"].split("\n", 1)[1] for call in FakeHandler.calls]
        self.assertIn("-TAIL", "".join(chunks))

    def test_disappeared_sources_do_not_erase_a_raw_snapshot(self):
        source = self.put_session("gone.jsonl", [
            {"type": "session", "id": "r", "cwd": str(self.project)},
            {"type": "message", "id": "u", "parentId": "r", "message": {"role": "user", "content": "kept raw"}},
        ])
        ms.process(str(self.project), self.bucket, self.output, self.router, False, None)
        raw = (self.output / "master-raw.jsonl").read_bytes()
        source.unlink()  # fixture only; the tool itself never removes sources
        with self.assertRaisesRegex(ValueError, "source sessions disappeared"):
            ms.process(str(self.project), self.bucket, self.output, self.router, False, None)
        self.assertEqual((self.output / "master-raw.jsonl").read_bytes(), raw)

    def test_overlapping_runs_fail_before_a_paid_call(self):
        self.output.mkdir()
        with (self.output / ".master.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, "already in progress"):
                ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
        self.assertEqual(FakeHandler.calls, [])

    def test_empty_project_exports_valid_session_without_calls(self):
        ms.process(str(self.project), self.bucket, self.output, self.router, True, None)
        self.assertEqual(FakeHandler.calls, [])
        rows = [json.loads(line) for line in (self.output / "mastersession.jsonl").read_text().splitlines()]
        self.assertEqual([row["type"] for row in rows], ["session", "session_info"])


if __name__ == "__main__":
    unittest.main()
