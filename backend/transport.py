"""Wire layer: the usage snapshot Pi appends, upstream headers, auth, request gzip.

Nothing here inspects conversation content — these functions move bytes and credentials
between Pi, the router and the inference host without changing what a model sees.
"""
from __future__ import annotations

import gzip
import json
import re
from typing import Any

from .config import (AUTH_PATH, GZIP_MIN_BYTES, GZIP_PROVIDERS, HOP_HEADERS,
                     MANAGED_AUTH)

SNAPSHOT_RE = re.compile(r"_pi_ctx::(\{.*\})", re.DOTALL)


def parse_snapshot(text: str) -> dict[str, Any]:
    """Split a raw body from the `_pi_ctx` usage snapshot Pi appends to it.

    Usage must come from the host's own report: the char estimate is only good enough to
    choose drop victims, and trimming on it would cut messages the host already cached.
    """
    if not (found := SNAPSHOT_RE.search(text)):
        raise ValueError("no _pi_ctx snapshot marker in request body")
    try:
        data = json.loads(found.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"snapshot payload is not valid JSON: {exc}") from exc
    if not isinstance(usage := data.get("usage") or data.get("tokens"), int) or usage <= 0:
        raise ValueError("snapshot has no integer usage")
    return {"usage": usage, "body": json.loads(text[:found.start()])}


def auth_map() -> dict[str, str]:
    """provider -> bearer key from auth.json; a missing file or entry is fine."""
    try:
        raw = json.loads(AUTH_PATH.read_text())
    except (OSError, ValueError):
        return {}
    return {provider: key for provider, entry in raw.items() if isinstance(entry, dict)
            and isinstance(key := entry.get("key") or entry.get("apiKey") or entry.get("access"),
                           str) and key}


def upstream_headers(provider: str, headers, auth: dict[str, str]) -> dict[str, str]:
    """Strip hop headers; managed providers get router-injected auth, not client auth."""
    out = {key: value for key, value in headers.items() if key.lower() not in HOP_HEADERS}
    lowered = {key.lower() for key in out}
    if "content-type" not in lowered:
        out["Content-Type"] = "application/json"
    if "accept-encoding" not in lowered:
        out["Accept-Encoding"] = "gzip, deflate"
    if provider not in auth:
        return out
    return {**{key: value for key, value in out.items() if key.lower() not in MANAGED_AUTH},
            "Authorization": f"Bearer {auth[provider]}"}


def maybe_gzip(provider: str, body: bytes, headers: dict[str, str]) -> bytes:
    """Gzip big cloud-bound bodies; edges decode them, local hosts cannot."""
    if provider in GZIP_PROVIDERS and len(body) >= GZIP_MIN_BYTES:
        headers["Content-Encoding"], body = "gzip", gzip.compress(body)
    return body
