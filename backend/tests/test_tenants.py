#!/usr/bin/env python3
"""Focused checks for the multi-tenant router: the seeded default account, session login, per-tenant
config/ledger/counter isolation, operator-only endpoints and tenant proxy tokens."""
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

# Auth stays ON here (that is the point of this suite); only the provider probes are network calls.
os.environ["PI_CONTEXT_ROUTER_PROVIDER_PROBE_TTL"] = "0"
os.environ["PI_CONTEXT_ROUTER_REQUIRE_LOGIN"] = "1"
WORKDIR = tempfile.mkdtemp(prefix="ctx-tenants-")
atexit.register(shutil.rmtree, WORKDIR, ignore_errors=True)
os.environ["PI_CONTEXT_ROUTER_DB"] = os.path.join(WORKDIR, "ctx-engine.db")

MODULE_PATH = Path(__file__).resolve().parents[1] / "ctx_engine_server.py"
SPEC = importlib.util.spec_from_file_location("tenant_router", MODULE_PATH)
router = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(router)


class TenantTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="ctx-tenant-test-", dir=WORKDIR)
        router.DB_PATH = os.path.join(self.tmpdir, "ctx-engine.db")
        router.TENANTS_READY["path"] = None
        router.TENANT_METRICS.clear()
        router.set_tenant(router.DEFAULT_USER_ID)
        with router.LOGIN_LOCK:
            router.LOGIN_ATTEMPTS.clear()
        router.PUBLIC_DEPLOY = False

    def tearDown(self):
        router.PUBLIC_DEPLOY = False
        router.set_tenant(router.DEFAULT_USER_ID)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def start_server(self):
        server = router.make_server("127.0.0.1", 0, "https://upstream.invalid")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    @staticmethod
    def call(server, method, path, body=None, headers=None):
        client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
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

    def test_default_tenant_is_seeded_as_nikhil(self):
        account = router.default_user()
        self.assertIsNotNone(account, "the first database seeds the operator account")
        self.assertEqual(account["id"], router.DEFAULT_USER_ID)
        self.assertEqual(account["username"], "Nikhil")
        self.assertTrue(router.verify_password(router.find_user("Nikhil")["password"], "Nikhil"),
                        "the seeded password is a PBKDF2 hash, not the text")
        logged_in = router.authenticate("Nikhil", "Nikhil")
        self.assertEqual(logged_in["id"], router.DEFAULT_USER_ID)
        self.assertIsNone(router.authenticate("Nikhil", "wrong"), "a wrong password never opens a session")

    def test_sessions_identify_one_account_and_expire(self):
        account = router.default_user()
        token = router.create_session(account["id"])
        self.assertEqual(router.session_user(token)["username"], "Nikhil")
        conn = router.db_connect()
        try:
            conn.execute("UPDATE sessions SET expires_at = 1 WHERE token = ?", (token,))
            conn.commit()
        finally:
            conn.close()
        self.assertIsNone(router.session_user(token), "an expired session is dropped, not honored")
        router.destroy_session(token)
        self.assertIsNone(router.session_user(token))

    def test_logout_works_without_a_body_and_ends_the_session(self):
        server = self.start_server()
        token = router.create_session(router.default_user()["id"])
        # A bare `curl -X POST /api/logout` carries no body and must still end the session.
        status, _raw, cookie_header = self.call(server, "POST", "/api/logout", headers=self.cookie(token))
        self.assertEqual(status, 200)
        self.assertIn("Max-Age=0", cookie_header or "")
        self.assertEqual(self.call(server, "GET", "/stats", headers=self.cookie(token))[0], 401)

    def test_dashboard_reads_require_a_login_and_answer_one_tenant(self):
        server = self.start_server()
        status, _raw, _cookie = self.call(server, "GET", "/stats")
        self.assertEqual(status, 401, "no session, no dashboard")
        token = router.create_session(router.default_user()["id"])
        status, raw, _cookie = self.call(server, "GET", "/stats", headers=self.cookie(token))
        self.assertEqual(status, 200)
        stats = json.loads(raw)
        self.assertEqual(stats["user"]["username"], "Nikhil")
        self.assertTrue(stats["user"]["operator"])
        self.assertEqual(stats["provider"], router.user_settings(router.DEFAULT_USER_ID)["provider"])
        status, raw, _cookie = self.call(server, "GET", "/health")
        self.assertEqual(status, 200, "health stays public")
        status, _raw, _cookie = self.call(server, "GET", "/dashboard")
        self.assertEqual(status, 200, "the login screen's assets stay public")

    def test_register_gives_each_account_its_own_settings_and_ledger(self):
        server = self.start_server()
        status, raw, cookie_header = self.call(server, "POST", "/api/register",
                                               {"username": "alice", "password": "secret-1"})
        self.assertEqual(status, 200, raw)
        self.assertTrue((cookie_header or "").startswith(f"{router.SESSION_COOKIE}="), "register logs the account in")
        alice = json.loads(raw)["user"]
        self.assertFalse(alice["operator"])
        alice_cookie = self.cookie(cookie_header.split("=", 1)[1].split(";")[0])

        status, _raw, _cookie = self.call(server, "POST", "/config", {"tokens": 8192, "summary_words": 100},
                                          headers=alice_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(router.user_settings(alice["id"])["tokens"], 8192)
        self.assertEqual(router.user_settings(alice["id"])["summary_words"], 100)
        operator = router.user_settings(router.DEFAULT_USER_ID)
        self.assertEqual(operator["tokens"], router.CONTEXT_TOKENS, "alice's sliders never move Nikhil's")

        router.set_tenant(alice["id"])
        router.record_request({"at": time.time(), "route": "local", "result": "compacted",
                               "raw_tokens": 100, "sent_tokens": 40})
        router.bump(requests=1, compacted=1)
        router.set_tenant(router.DEFAULT_USER_ID)
        router.record_request({"at": time.time(), "route": "local", "result": "passed",
                               "raw_tokens": 100, "sent_tokens": 100})
        router.bump(requests=1, passed=1)
        self.assertEqual(router.global_stats(alice["id"])["size"], 1)
        self.assertEqual(router.global_stats(router.DEFAULT_USER_ID)["size"], 1)
        row_id = router.recent_requests(1, alice["id"])[0]["id"]
        self.assertIsNone(router.request_detail(row_id, router.DEFAULT_USER_ID),
                          "one tenant can never read another tenant's request")
        self.assertIsNotNone(router.request_detail(row_id, alice["id"]))
        self.assertEqual(router.stats_snapshot(alice["id"])["requests"], 1)
        self.assertEqual(router.stats_snapshot(router.DEFAULT_USER_ID)["requests"], 1)
        # The HTTP view agrees with the direct reads.
        token = router.create_session(alice["id"])
        status, raw, _cookie = self.call(server, "GET", "/stats", headers=self.cookie(token))
        stats = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(stats["global"]["size"], 1)
        self.assertEqual(stats["token_limit"], 8192, "the dashboard shows this account's own settings")
        self.assertEqual(stats["user"]["username"], "alice")

    def test_operator_endpoints_are_the_default_tenants_only(self):
        server = self.start_server()
        router.create_user("bob", "secret-2")
        bob = router.authenticate("bob", "secret-2")
        bob_cookie = self.cookie(router.create_session(bob["id"]))
        status, _raw, _cookie = self.call(server, "GET", "/debug/projects", headers=bob_cookie)
        self.assertEqual(status, 403, "another account never browses the operator's sessions")
        status, _raw, _cookie = self.call(server, "GET", "/debug/projects")
        self.assertEqual(status, 200, "loopback trust model still answers the operator's CLI")
        router.PUBLIC_DEPLOY = True
        status, _raw, _cookie = self.call(server, "GET", "/debug/projects")
        self.assertEqual(status, 401, "an exposed router closes the anonymous operator endpoints")
        nikhil_cookie = self.cookie(router.create_session(router.DEFAULT_USER_ID))
        status, _raw, _cookie = self.call(server, "GET", "/debug/projects", headers=nikhil_cookie)
        self.assertEqual(status, 200, "the operator still gets through")

    def test_proxy_paths_carry_a_tenant_token(self):
        server = self.start_server()
        router.create_user("carol", "secret-3")
        carol = router.authenticate("carol", "secret-3")
        status, _raw, _cookie = self.call(server, "GET", f"/t/{carol['api_token']}/nope")
        self.assertEqual(status, 404, "a valid tenant token strips the prefix and reaches the router")
        status, _raw, _cookie = self.call(server, "GET", "/t/not-a-real-token-123/nope")
        self.assertEqual(status, 401, "an unknown tenant token is rejected")
        status, _raw, _cookie = self.call(server, "GET", "/nope")
        self.assertEqual(status, 404, "bare paths stay the operator's router while the deployment is local")
        router.PUBLIC_DEPLOY = True
        status, _raw, _cookie = self.call(server, "GET", "/nope")
        self.assertEqual(status, 401, "an exposed deployment refuses bare proxy paths")
        status, _raw, _cookie = self.call(server, "GET", f"/t/{carol['api_token']}/nope")
        self.assertEqual(status, 404, "the tenant path keeps working when the deployment is public")

    def test_login_attempts_are_throttled(self):
        router.create_user("dave", "secret-4")
        for _ in range(router.LOGIN_MAX_ATTEMPTS):
            self.assertIsNone(router.authenticate("dave", "nope"))
        self.assertIsNone(router.authenticate("dave", "secret-4"), "the name is locked out, even correct")
        with router.LOGIN_LOCK:
            router.LOGIN_ATTEMPTS.clear()
        self.assertIsNotNone(router.authenticate("dave", "secret-4"))

    def test_passwords_are_hashed_not_stored(self):
        router.create_user("erin", "secret-5")
        stored = router.find_user("erin")["password"]
        self.assertTrue(stored.startswith("pbkdf2_sha256$"), "the database never holds the password")
        self.assertNotIn("secret-5", stored)


if __name__ == "__main__":
    unittest.main(verbosity=2)
