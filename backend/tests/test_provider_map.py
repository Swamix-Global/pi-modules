#!/usr/bin/env python3
"""Focused checks for the Pi provider map: catalog-to-chat translation, credential discovery,
family/cost model ordering, the reason a provider cannot work, and probe result caching."""
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

# Probes hit the network; the map's static checks are what these tests answer, so keep them off.
os.environ["PI_CONTEXT_ROUTER_PROVIDER_PROBE_TTL"] = "0"
MODULE_PATH = Path(__file__).resolve().parents[1] / "ctx_engine_server.py"
SPEC = importlib.util.spec_from_file_location("provider_map_router", MODULE_PATH)
router = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(router)


def model(ident, api="openai-completions", base="https://api.example.com/v1", cost=1.0, **extra):
    entry = {"id": ident, "name": ident, "api": api, "baseUrl": base, "type": "chat",
             "input": ["text"], "cost": {"input": cost, "output": cost * 2}}
    entry.update(extra)
    return entry


class ProviderMapTests(unittest.TestCase):
    def test_registry_maps_at_least_the_ten_most_popular_providers(self):
        ids = [pid for pid, _label, _env, _family in router.PI_PROVIDERS]
        self.assertGreaterEqual(len(ids), 10)
        self.assertEqual(len(ids), len(set(ids)), "provider ids must be unique")
        for pid, label, env, _family in router.PI_PROVIDERS:
            self.assertTrue(label and env == env.upper(), f"{pid} needs a label and an env var")
        selectable = set(router.provider_ids())
        self.assertTrue({"deepseek", "local", "openai-codex"} <= selectable)
        self.assertTrue(set(ids) <= selectable, "every mapped major provider must be selectable")

    def test_catalog_api_styles_translate_to_chat_completion_urls(self):
        self.assertEqual(router.chat_completion_url(model("a")), "https://api.example.com/v1/chat/completions")
        gemini = model("g", api="google-generative-ai", base="https://generativelanguage.googleapis.com/v1beta")
        self.assertEqual(router.chat_completion_url(gemini),
                         "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions")
        self.assertIsNone(router.chat_completion_url(model("c", api="anthropic-messages", base="https://api.anthropic.com")))
        self.assertIsNone(router.chat_completion_url(model("n", base="")))

    def test_candidates_prefer_the_providers_own_family_then_cheapest(self):
        models = [model("third-party", cost=0), model("qwen3.5-9b", cost=5), model("other", cost=9)]
        ordered = [item["id"] for item in router.condenser_models(models, ("qwen",))]
        self.assertEqual(ordered[0], "qwen3.5-9b", "a gated plan unlocks its own family first")
        self.assertEqual([item["id"] for item in router.condenser_models(models)], ["third-party", "qwen3.5-9b", "other"])

    def test_candidates_skip_aliases_free_markers_and_unsupported_styles(self):
        models = [model("~anthropic/claude-fake"), model("auto", cost=-1000000),
                  model("claude-4", api="anthropic-messages"), model("gpt-4.1")]
        self.assertEqual([item["id"] for item in router.condenser_models(models)], ["gpt-4.1"])
        self.assertEqual(router.condenser_models([model("embed", input=["image"])]), [])

    def test_status_names_the_first_real_reason_a_provider_cannot_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "auth.json")
            catalog = os.path.join(tmp, "models-store.json")

            def write(path, value):
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(value, handle)

            # No credential at all.
            write(auth, {})
            write(catalog, {"groq": {"models": [model("llama")]}})
            row = router.provider_status("groq", "Groq", "GROQ_TEST_KEY", auth, catalog)
            self.assertFalse(row["available"])
            self.assertIn("no credential", row["error"])
            # Credential but nothing callable in the cached catalog.
            write(auth, {"groq": {"key": "k"}})
            write(catalog, {})
            row = router.provider_status("groq", "Groq", "", auth, catalog)
            self.assertFalse(row["available"])
            self.assertIn("catalog", row["error"])
            write(auth, {"groq": {"key": "k"}, "anthropic": {"key": "k"}})
            # Credential, catalog, but an API style the condenser cannot speak.
            write(catalog, {"anthropic": {"models": [model("claude-4", api="anthropic-messages",
                                                            base="https://api.anthropic.com")]}})
            row = router.provider_status("anthropic", "Anthropic", "", auth, catalog)
            self.assertFalse(row["available"])
            self.assertIn("not supported", row["error"])
            # Everything present: working.
            write(catalog, {"groq": {"models": [model("llama-3.3-70b")]}})
            row = router.provider_status("groq", "Groq", "", auth, catalog)
            self.assertTrue(row["available"], row["error"])
            self.assertEqual(row["model"], "llama-3.3-70b")

    def test_extra_installed_provider_joins_the_map_after_the_majors(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "auth.json")
            catalog = os.path.join(tmp, "models-store.json")
            with open(auth, "w", encoding="utf-8") as handle:
                json.dump({"my-proxy": {"key": "k"}}, handle)
            with open(catalog, "w", encoding="utf-8") as handle:
                json.dump({"my-proxy": {"models": [model("m")]}}, handle)
            rows = router.pi_provider_map(auth, catalog)
            self.assertEqual([row["provider"] for row in rows][:len(router.PI_PROVIDERS)],
                             [pid for pid, *_ in router.PI_PROVIDERS])
            extra = rows[-1]
            self.assertEqual(extra["provider"], "my-proxy")
            self.assertTrue(extra["available"], extra["error"])
            self.assertEqual(extra["label"], "My Proxy")

    def test_provider_endpoint_returns_the_catalog_url_and_bearer(self):
        with tempfile.TemporaryDirectory() as tmp:
            auth = os.path.join(tmp, "auth.json")
            catalog = os.path.join(tmp, "models-store.json")
            with open(auth, "w", encoding="utf-8") as handle:
                json.dump({"groq": {"key": "secret"}}, handle)
            with open(catalog, "w", encoding="utf-8") as handle:
                json.dump({"groq": {"models": [model("llama")]}}, handle)
            url, headers = router.provider_endpoint("groq", "llama", auth, catalog)
            self.assertEqual(url, "https://api.example.com/v1/chat/completions")
            self.assertEqual(headers["Authorization"], "Bearer secret")
            with self.assertRaises(router.CondenserError):
                router.provider_endpoint("groq", "llama", os.path.join(tmp, "empty.json"), catalog)

    def test_probe_results_are_cached_and_refreshed_in_the_background(self):
        calls = []

        def fake_probe(pid):
            calls.append(pid)
            return {"provider": pid, "model": "m", "available": True, "error": ""}

        class InlinePool:
            def submit(self, fn, *args):
                fn(*args)

        original_probe, original_pool, original_ttl = router._probe_provider, router.PROBE_POOL, router.PROBE_TTL
        router._probe_provider, router.PROBE_POOL, router.PROBE_TTL = fake_probe, InlinePool(), 600.0
        router.PROBES.pop("probe-test", None)
        try:
            self.assertIsNone(router._probe_state("probe-test"), "first sight only starts the probe")
            self.assertEqual(router._probe_state("probe-test")["model"], "m", "cached answer serves immediately")
            self.assertEqual(calls, ["probe-test"], "a fresh answer is never re-probed")
            with router.PROBE_LOCK:
                router.PROBES["probe-test"]["at"] = time.monotonic() - 601
            self.assertEqual(router._probe_state("probe-test")["model"], "m", "stale answer still serves")
            self.assertEqual(calls, ["probe-test", "probe-test"], "a stale answer triggers a refresh")
        finally:
            router._probe_provider, router.PROBE_POOL, router.PROBE_TTL = original_probe, original_pool, original_ttl
            router.PROBES.pop("probe-test", None)

    def test_static_map_is_the_answer_when_probing_is_off(self):
        rows = router.mapped_provider_rows()
        self.assertTrue(all("error" in row and "available" in row for row in rows))
        self.assertNotIn("checking provider…", " ".join(row["error"] for row in rows))
        for row in rows:
            if row["available"]:
                self.assertTrue(router.provider_ready(row["provider"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
