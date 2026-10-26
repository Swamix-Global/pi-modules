"""Wire-layer tests: the `_pi_ctx` snapshot split, header hygiene, opt-in request gzip."""
from __future__ import annotations

import gzip as gz
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import transport as t  # noqa: E402


def snapshot(body: dict, usage: int) -> str:
    return json.dumps(body) + f'\n\n_pi_ctx::{{"usage": {usage}}}'


def test_snapshot_parse_roundtrip():
    parsed = t.parse_snapshot(snapshot({"messages": [{"role": "user", "content": "hi"}]}, 42000))
    assert parsed["usage"] == 42000
    assert parsed["body"]["messages"][0]["content"] == "hi"
    assert "messages" in parsed["body"] and "_pi_ctx" not in json.dumps(parsed["body"])


def test_snapshot_missing_marker_raises():
    with pytest.raises(ValueError):
        t.parse_snapshot(json.dumps({"messages": []}))


def test_snapshot_malformed_json_raises():
    with pytest.raises(ValueError):
        t.parse_snapshot('{"messages": []}\n\n_pi_ctx::{"usage": "lots"}')


def test_maybe_gzip_only_for_cloud_providers_and_large_bodies(monkeypatch):
    monkeypatch.setattr(t, "GZIP_PROVIDERS", {"deepseek"})
    big = b"x" * (t.GZIP_MIN_BYTES + 10)
    headers = {}
    body = t.maybe_gzip("deepseek", big, headers)
    assert headers["Content-Encoding"] == "gzip" and body != big
    assert t.maybe_gzip("sglang", big, {}) is big, "locals stay raw"
    assert t.maybe_gzip("deepseek", b"small", {}) == b"small", "small bodies stay raw"
    assert gz.decompress(body) == big


def test_managed_provider_gets_router_auth_only():
    headers = {"Authorization": "Bearer client", "X-Api-Key": "client", "Content-Type": "application/json"}
    out = t.upstream_headers("deepseek", headers, {"deepseek": "router-key"})
    assert out["Authorization"] == "Bearer router-key"
    assert t.upstream_headers("lmstudio", headers, {})["Authorization"] == "Bearer client"
