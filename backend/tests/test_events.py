#!/usr/bin/env python3
"""The dashboard push channel: `/api/events` frames arrive after real state changes, stay scoped to the
tenant that changed, and keep the socket alive with a heartbeat when nothing happens."""
import atexit
import http.client
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

# Auth stays ON: the push channel is only meaningful for a signed-in dashboard.
os.environ["PI_CONTEXT_ROUTER_PROVIDER_PROBE_TTL"] = "0"
os.environ["PI_CONTEXT_ROUTER_REQUIRE_LOGIN"] = "1"
WORKDIR = tempfile.mkdtemp(prefix="ctx-events-")
atexit.register(shutil.rmtree, WORKDIR, ignore_errors=True)
os.environ["PI_CONTEXT_ROUTER_DB"] = os.path.join(WORKDIR, "ctx-engine.db")

MODULE_PATH = Path(__file__).resolve().parents[1] / "ctx_engine_server.py"
SPEC = importlib.util.spec_from_file_location("event_router", MODULE_PATH)
router = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(router)


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ctx-event-test-", dir=WORKDIR)
        router.DB_PATH = os.path.join(self.tmpdir, "ctx-engine.db")
        router.TENANTS_READY["path"] = None
        router.TENANT_METRICS.clear()
        router.set_tenant(router.DEFAULT_USER_ID)
        with router.LIVE:
            router.LIVE_GEN.clear()
        router.PUBLIC_DEPLOY = False
        self.server = router.make_server("127.0.0.1", 0, "https://upstream.invalid")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def tearDown(self):
        router.set_tenant(router.DEFAULT_USER_ID)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ----------------------------- helpers -----------------------------
    def call(self, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        request_headers = dict(headers or {})
        if payload is not None:
            request_headers.setdefault("Content-Type", "application/json")
        client.request(method, path, body=payload, headers=request_headers)
        response = client.getresponse()
        raw = response.read()
        client.close()
        return response.status, raw, response.getheader("Set-Cookie")

    @staticmethod
    def cookie(token):
        return {"Cookie": f"{router.SESSION_COOKIE}={token}"}

    @staticmethod
    def token_from(cookie_header):
        return (cookie_header or "").split("=", 1)[1].split(";", 1)[0]

    def subscribe(self, headers):
        """Open the SSE stream: returns (connection, response) with the response headers already read."""
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        client.request("GET", "/api/events", headers=headers)
        response = client.getresponse()
        self.addCleanup(client.close)
        return client, response

    def next_frame(self, client, response, timeout=5.0):
        """Bytes until one complete SSE frame, or "" when the channel stays silent that long."""
        if client.sock is not None:
            client.sock.settimeout(timeout)
        deadline = time.monotonic() + timeout
        buffer = bytearray()
        try:
            while time.monotonic() < deadline:
                chunk = response.read(1)
                if not chunk:
                    break
                buffer += chunk
                if buffer.endswith(b"\n\n"):
                    return buffer.decode("utf-8", "replace")
        except OSError:
            pass  # a silent channel is exactly what the caller is asserting
        return ""

    def session(self, username=None):
        if username is None:
            token = router.create_session(router.DEFAULT_USER_ID)
        else:
            status, _raw, header = self.call("POST", "/api/register",
                                             {"username": username, "password": "hunter2hunter2"})
            self.assertEqual(status, 200, "open registration is on with auth enabled")
            token = self.token_from(header)
        return self.cookie(token)

    # ----------------------------- tests -----------------------------
    def test_events_need_a_session(self):
        status, raw, _header = self.call("GET", "/api/events")
        self.assertEqual(status, 401, "an anonymous reader never holds a push channel open")
        self.assertIn(b"authentication required", raw)

    def test_config_write_pushes_one_frame_to_the_writing_tenant(self):
        headers = self.session()
        client, response = self.subscribe(headers)
        self.assertIn("event: ready", self.next_frame(client, response), "the stream opens immediately")

        status, _raw, _header = self.call("POST", "/config", {"tokens": 131072}, headers)
        self.assertEqual(status, 200)
        frame = self.next_frame(client, response)
        self.assertIn("event: change", frame, "a saved setting wakes the dashboard that saved it")

    def test_counter_and_ledger_bumps_wake_the_subscriber(self):
        headers = self.session()
        client, response = self.subscribe(headers)
        self.assertIn("event: ready", self.next_frame(client, response))

        router.record_request({"route": "/r/deepseek", "model": "m", "result": "passed",
                               "user_id": router.DEFAULT_USER_ID})
        self.assertIn("event: change", self.next_frame(client, response), "one routed request pushes a frame")

        router.bump(requests=1, passed=1)
        self.assertIn("event: change", self.next_frame(client, response), "a counter bump pushes a frame")

    def test_a_change_never_reaches_another_tenant(self):
        operator = self.session()
        tenant = self.session("Bobby")
        op_client, op_response = self.subscribe(operator)
        tenant_client, tenant_response = self.subscribe(tenant)
        self.assertIn("event: ready", self.next_frame(op_client, op_response))
        self.assertIn("event: ready", self.next_frame(tenant_client, tenant_response))

        status, _raw, _header = self.call("POST", "/config", {"tokens": 65536}, tenant)
        self.assertEqual(status, 200)
        self.assertIn("event: change", self.next_frame(tenant_client, tenant_response),
                      "the tenant that changed hears about it first")
        self.assertEqual("", self.next_frame(op_client, op_response, 1.0),
                         "the operator's dashboard must not refetch on another tenant's change")

    def test_heartbeat_keeps_an_idle_channel_open(self):
        heartbeat, router.LIVE_HEARTBEAT_S = router.LIVE_HEARTBEAT_S, 0.2
        try:
            headers = self.session()
            client, response = self.subscribe(headers)
            self.assertIn("event: ready", self.next_frame(client, response))
            self.assertIn(": ping", self.next_frame(client, response, 2.0),
                          "an idle subscriber still hears the heartbeat")
        finally:
            router.LIVE_HEARTBEAT_S = heartbeat


if __name__ == "__main__":
    unittest.main()
