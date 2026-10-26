"""Every router knob in one place: read once at import, never per request.

Environment names are the only supported override; a unit restart is the only way to
change routing behaviour. Values are plain module constants so the hot path pays no
attribute lookups and tests can monkeypatch the owning module.
"""
from __future__ import annotations

import json
import os
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def _text(name: str, default: str) -> str:
    return os.environ.get(name) or default


# --- minification ------------------------------------------------------------------------
BUDGET = _int("PI_ROUTER_BUDGET", 60_000)             # usage that triggers minification
CUT_FRACTION = _float("PI_ROUTER_CUT_FRACTION", 0.4)  # hysteresis: cut down to this share
CONDENSE_TARGET_FRACTION = 0.2                        # condenser target, share of entry tokens
CHARS_PER_TOKEN = 3.6                                 # conservative estimate, cap decisions only
MAX_BODY_BYTES = _int("PI_ROUTER_MAX_BODY_BYTES", 128 << 20)  # bigger bodies pass untouched
FRONTIER_CAP = 64                                     # remembered conversations (LRU)
LEVEL = _text("PI_ROUTER_LEVEL", "L2")                # Fast Mode | L1 vs Standard Mode | L2

# --- local condenser ---------------------------------------------------------------------
SG_LANG_BASE = _text("SG_LANG_BASE", "http://127.0.0.1:30000/v1")
SG_LANG_MODEL = _text("SG_LANG_MODEL", "")            # blank: use whatever the host serves
CONDENSE_RETRIES = 2

# --- inference hosts ---------------------------------------------------------------------
UPSTREAM_TIMEOUT = (10, 300)                          # connect, read
GZIP_MIN_BYTES = 32 << 10
# Request gzip is opt-in: real cloud APIs reject Content-Encoding on POST /chat/completions
# with 400 'Failed to parse JSON', and local uvicorn hosts cannot decode it at all.
GZIP_PROVIDERS = frozenset(filter(None, _text("PI_ROUTER_GZIP_PROVIDERS", "").split(",")))
CODEX_HOST = _text("PI_ROUTER_CODEX_UPSTREAM",
                   "https://chatgpt.com/backend-api/codex/responses")
DEFAULT_ROUTES = {
    "sglang": SG_LANG_BASE,
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "qwen-token-plan": "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1",
    "xiaomi-token-plan-sgp": "https://token-plan-sgp.xiaomimimo.com/v1",
    "antigravity": "https://daily-cloudcode-pa.googleapis.com",
    "lmstudio": "http://127.0.0.1:1234/v1",
    "openai": "https://api.openai.com/v1",
    "zai-coding-cn": "https://api.z.ai/api/coding/paas/v4",
    "zai": "https://api.z.ai/api/coding/paas/v4",
}
ROUTES: dict[str, str] = json.loads(_text("PI_ROUTER_ROUTES", json.dumps(DEFAULT_ROUTES)))
HOP_HEADERS = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                         "te", "trailer", "transfer-encoding", "upgrade", "host",
                         "content-length"})
MANAGED_AUTH = frozenset({"api-key", "authorization"})  # router injects its own bearer

# --- service -----------------------------------------------------------------------------
HOST = _text("PI_ROUTER_HOST", "127.0.0.1")
PORT = _int("PI_ROUTER_PORT", 4141)
AUTH_PATH = Path(_text("PI_ROUTER_AUTH", "~/.pi/agent/auth.json")).expanduser()
CACHE_DIR = Path(_text("PI_ROUTER_CACHE", "~/.pi/agent/ctx-cache")).expanduser()
