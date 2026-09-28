#!/usr/bin/env python3
"""Loopback-only context router: provider proxy + settled-block condenser with a dashboard.

Two jobs, both outside Pi so the transcript is never touched:

1. **Proxy.** `/codex/responses` (and opt-in `/r/<provider>/...`) is forwarded upstream with the caller's
   authorization intact, streaming preserved, request encodings (identity/gzip/zstd) supported, images passed
   through untouched and never logged.
2. **Condense.** Above `PI_CONTEXT_ROUTER_TOKENS` estimated tokens (text only — image payloads are never
   measured), every *completed* turn block (a block
   runs from one user message to just before the next) is replaced by a one-line cached summary, while the
   newest `LIVE_BLOCKS` blocks (the previous turn and the live one) are passed through byte-exact, and a live turn too large for the
   budget has its own biggest text strings clipped from the middle rather than being refused or forwarded whole.
   Block identity is the SHA-256 of
   the canonical block JSON, so a summary is computed once per distinct block and then served from `ctx-engine.db`
   (SQLite, WAL): the cache survives restarts and is only extended, never cleared implicitly.

The condenser runs the DeepSeek Flash backend: the prompt-policy block that beat hard thinking
budgets in the benchmark (OODA/Pareto/MECE/BLUF + hard rules), a structured note under Task/Query / Actions Performed / Conclusion,
and temperature 0.1 (a reasoning model that over-thinks as it warms). Counters are visible at `/dashboard`
and `/stats`.

Condensers are not hard-coded: `/stats` maps Pi's major providers (auth.json credential + Pi's cached
`models-store.json` catalog, plus any other provider Pi holds both for), marks each row with the reason it
cannot work, and *verifies* the rest with one tiny completion probe per provider — a cached catalog model can
be retired, unpurchased or unbilled while the credential still looks valid. Only verified rows are selectable;
`PI_CONTEXT_ROUTER_PROVIDER_PROBE_TTL=0` turns probing off and lets the static check answer.

Multi-tenant SaaS: every dashboard call runs as one logged-in account (seeded on first start:
`Nikhil` / `Nikhil`, override with `PI_CONTEXT_ROUTER_ADMIN` / `PI_CONTEXT_ROUTER_ADMIN_PASSWORD`), each
account owns its own ledger, counters and compaction settings, and `/api/register` creates more. The
operator's account also owns the debugger, the local token journal and `POST /master/condense`; every other
account gets a 403 there. A tenant's Pi points at `/t/<api-token>/...` for its own proxy traffic. Set
`PI_CONTEXT_ROUTER_PUBLIC=1` when the router is reachable beyond this machine: bare `/r/...` proxy paths and
the anonymous operator calls are then refused. `PI_CONTEXT_ROUTER_REQUIRE_LOGIN=0` disables the login
entirely (tests and local tooling only). The note cache stays content-addressed and shared — a note answers
only for a byte-identical block.
"""

from __future__ import annotations

from collections import Counter

import argparse
import gzip
import hashlib
import hmac
import http.client
import inspect
import compression.zstd as zstd
import json
import mimetypes
import os
import random
import re
import secrets
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
from concurrent import futures
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# Pure helpers and config live in the utils module; re-export them so module callers and tests see one namespace.
# Make the sibling module importable however this file is launched (systemd, importlib by path, cwd elsewhere).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ctx_engine_utils import *  # noqa: E402,F401,F403


# ----------------------------- tenants -----------------------------
# Accounts and tenant scoping: the seeded operator account is tenant 1 (every legacy row keeps that id),
# each dashboard call runs as one logged-in account, and a tenant's Pi traffic carries its own token.
DEFAULT_USER_ID = 1
DEFAULT_USERNAME = os.environ.get("PI_CONTEXT_ROUTER_ADMIN", "Nikhil")
DEFAULT_PASSWORD = os.environ.get("PI_CONTEXT_ROUTER_ADMIN_PASSWORD", "Nikhil")
# PI_CONTEXT_ROUTER_REQUIRE_LOGIN=0 turns the dashboard login off (tests, local tooling); default is required.
# (PI_CONTEXT_ROUTER_AUTH is already taken: it points at Pi's auth.json.)
AUTH_ENABLED = os.environ.get("PI_CONTEXT_ROUTER_REQUIRE_LOGIN", "1").strip().lower() not in {"0", "off", "false", "no"}
# Set when the router is reachable beyond this machine (behind a reverse proxy): bare `/r/...` proxy paths
# and the operator endpoints then require a session or token. Unset keeps the loopback trust model.
PUBLIC_DEPLOY = os.environ.get("PI_CONTEXT_ROUTER_PUBLIC", "0").strip().lower() in {"1", "on", "true", "yes"}


# The prompt is configuration state, not request-ledger data. Its digest is part of every
# summary identity so changing it can never reuse a note written under another prompt.
DEFAULT_CONDENSER_PROMPT = CONDENSER_SYSTEM
CONDENSER_PROMPT = DEFAULT_CONDENSER_PROMPT
PROMPT_MAX_CHARS = 32 * 1024
PROMPT_LOCK = threading.RLock()


def condenser_prompt() -> str:
    with PROMPT_LOCK:
        return CONDENSER_PROMPT


def condenser_prompt_hash(prompt: str | None = None) -> str:
    value = condenser_prompt() if prompt is None else prompt
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def usage_add(tokens_in: int, tokens_out: int, usd: float) -> None:
    """Add one condenser call to the request being served on this thread."""
    if not getattr(REQUEST_USAGE, "active", False):
        return
    REQUEST_USAGE.in_tokens += tokens_in
    REQUEST_USAGE.out_tokens += tokens_out
    REQUEST_USAGE.usd += usd
    REQUEST_USAGE.calls += 1


def usage_start() -> None:
    """Fresh condenser accumulator: one ThreadingHTTPServer thread serves exactly one request."""
    REQUEST_USAGE.active = True
    REQUEST_USAGE.in_tokens = 0
    REQUEST_USAGE.out_tokens = 0
    REQUEST_USAGE.usd = 0.0
    REQUEST_USAGE.calls = 0


def usage_snapshot() -> dict:
    return {"in": REQUEST_USAGE.in_tokens, "out": REQUEST_USAGE.out_tokens, "usd": REQUEST_USAGE.usd, "calls": REQUEST_USAGE.calls}


# ----------------------------- metrics -----------------------------

METRICS = {
    "requests": 0, "passed": 0, "compacted": 0, "images": 0, "overflow": 0, "trimmed": 0,
    "blocks_seen": 0, "blocks_summarized": 0, "blocks_pending": 0, "blocks_fallback": 0, "blocks_dropped": 0, "cache_hits": 0,
    "condenser_errors": 0, "condenser_calls_deepseek": 0, "condenser_calls_local": 0,
    "condenser_calls_openai_codex": 0, "condenser_ms_total": 0.0,
    "condenser_tokens_in": 0, "condenser_tokens_out": 0, "condenser_cost_usd": 0.0,
    "input_chars_raw": 0, "input_chars_sent": 0, "started": time.time(),
}
LAST_COMPACTION = {"at": 0.0}
METRICS_LOCK = threading.Lock()
# Counters are per tenant: the default tenant owns METRICS (what a restart and the test suite reset),
# every other tenant gets a zeroed copy of it, and a background job carries its tenant on the thread.
TENANT = threading.local()
TENANT_METRICS: dict[int, dict] = {}
METRICS_ZERO: dict = {key: (0.0 if isinstance(value, float) else 0) for key, value in METRICS.items()}
REQUEST_USAGE = threading.local()
DB_LOCK = threading.Lock()
DB_ERROR = {"at": 0.0}
# The compaction threshold is read at request time, never frozen into a signature, so the dashboard can move it
# without a restart; a restart falls back to PI_CONTEXT_ROUTER_TOKENS.
BUDGET = {"tokens": CONTEXT_TOKENS, "set_at": 0.0}
# Provider selection is durable config, not request data. A summary job receives an immutable snapshot.
# The three core backends are always on the dashboard; `provider_ids()` extends them with Pi's mapped
# providers (auth.json credential + models-store.json catalog), so the map follows what is installed.
CORE_PROVIDERS = ("deepseek", "local", "openai-codex")
RANDOM_PROVIDER = "random"
PROVIDER_LOCK = threading.RLock()
ACTIVE_PROVIDER = "deepseek"
# The provider a tenant without a saved choice starts on: the boot default, not the operator's current pick.
BOOT_PROVIDER = ACTIVE_PROVIDER
CODEX_HELPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "condenser_bridge.mjs")
BRIDGE_INPUT_MAX_BYTES = 8 * 1024 * 1024
BRIDGE_OUTPUT_MAX_BYTES = 256 * 1024
BRIDGE_TIMEOUT = float(os.environ.get("PI_CONTEXT_ROUTER_BRIDGE_TIMEOUT", "60"))
CODEX_DISCOVERY = {"available": False, "provider": "openai-codex", "model": "gpt-6-luna", "error": "Codex Luna availability has not been checked"}
CODEX_DISCOVERY_AT = 0.0
CODEX_DISCOVERY_LOCK = threading.Lock()


def _bridge(action: str, payload: dict | None = None, timeout: float | None = None) -> dict:
    request = {"action": action, **(payload or {})}
    wire = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(wire) > BRIDGE_INPUT_MAX_BYTES:
        raise CondenserError("condenser request is too large")
    try:
        completed = subprocess.run(
            ["node", CODEX_HELPER], input=wire, capture_output=True,
            timeout=BRIDGE_TIMEOUT if timeout is None else timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CondenserError("condenser bridge unavailable") from exc
    if len(completed.stdout) > BRIDGE_OUTPUT_MAX_BYTES:
        raise CondenserError("condenser bridge response is too large")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CondenserError("condenser bridge returned invalid data") from exc
    if not isinstance(result, dict):
        raise CondenserError("condenser bridge returned invalid data")
    return result


def codex_discovery(force: bool = False) -> dict:
    global CODEX_DISCOVERY_AT, CODEX_DISCOVERY
    now = time.monotonic()
    with CODEX_DISCOVERY_LOCK:
        if not force and now - CODEX_DISCOVERY_AT < 30:
            return dict(CODEX_DISCOVERY)
        try:
            result = _bridge("discover", timeout=15)
            if result.get("ok") and result.get("provider") == "openai-codex" and result.get("model") == "gpt-6-luna":
                CODEX_DISCOVERY = {"available": bool(result.get("available")), "provider": "openai-codex", "model": "gpt-6-luna", "error": str(result.get("error") or "")}
            else:
                CODEX_DISCOVERY = {"available": False, "provider": "openai-codex", "model": "gpt-6-luna", "error": "Codex Luna is unavailable"}
        except CondenserError:
            CODEX_DISCOVERY = {"available": False, "provider": "openai-codex", "model": "gpt-6-luna", "error": "Codex Luna is unavailable"}
        CODEX_DISCOVERY_AT = now
        return dict(CODEX_DISCOVERY)


def local_model() -> str:
    try:
        with open(os.path.expanduser("~/.pi/agent/sgai-config.json"), encoding="utf-8") as handle:
            value = json.load(handle).get("localModel")
        return str(value) if isinstance(value, str) and value else "granite-4.2-3b-fp8"
    except (OSError, TypeError, ValueError):
        return "granite-4.2-3b-fp8"


# ----------------------------- provider verification -----------------------------
# Availability is verified, not assumed: a catalog model can be retired, unpurchased or unbilled while the
# credential still looks valid (Groq's cached model 404s, Gemini returns 402, Qwen's plan denies 403). One
# tiny completion probes each candidate, cheapest model first; the answer is cached for PROBE_TTL seconds,
# refreshed in the background while the last known answer keeps serving, and /stats never waits on a probe.
# Setting PROBE_TTL=0 turns probing off (tests, offline): the static credential + catalog check answers.
PROBE_TTL = float(os.environ.get("PI_CONTEXT_ROUTER_PROVIDER_PROBE_TTL", "600"))
PROBE_ATTEMPTS = 4
PROBE_TIMEOUT = min(CONDENSER_TIMEOUT, 20.0)
PROBES: dict[str, dict] = {}
PROBE_LOCK = threading.Lock()
PROBE_POOL = futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="ctx-probe")


def _probe_provider(pid: str) -> dict:
    """Ask one candidate model at a time until one answers; the last failure text is the row's error."""
    row = {"provider": pid, "model": "", "available": False, "error": "no catalog model to probe"}
    for candidate in provider_candidates(pid)[:PROBE_ATTEMPTS]:
        model = str(candidate["id"])
        row["model"] = model
        try:
            url, headers = provider_endpoint(pid, model)
            payload = http_json(url, {"model": model, "messages": [{"role": "user", "content": "Reply with OK."}]},
                                headers, PROBE_TIMEOUT)
            if condenser_text(payload):
                return {"provider": pid, "model": model, "available": True, "error": ""}
            row["error"] = "probe returned no usable text"
        except (CondenserError, OSError, ValueError) as exc:
            row["error"] = str(exc)[:300] or type(exc).__name__
    return row


def _run_probe(pid: str) -> None:
    try:
        row = _probe_provider(pid)
    except Exception as exc:  # a probe must degrade to "unavailable", never take the worker down
        row = {"provider": pid, "model": "", "available": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
    with PROBE_LOCK:
        PROBES.setdefault(pid, {"at": 0.0, "row": None, "busy": False}).update(at=time.monotonic(), row=row, busy=False)


def _probe_state(pid: str) -> dict | None:
    """Verified row for one mapped provider: None until the first probe answers, then cached for
    PROBE_TTL and refreshed in the background while the stale answer keeps serving."""
    if PROBE_TTL <= 0:
        return None
    spawn = False
    with PROBE_LOCK:
        entry = PROBES.setdefault(pid, {"at": 0.0, "row": None, "busy": False})
        if entry["busy"] or entry["row"] is None or time.monotonic() - entry["at"] >= PROBE_TTL:
            if not entry["busy"]:
                entry["busy"] = True
                spawn = True
        row = dict(entry["row"]) if entry["row"] else None
    if spawn:
        PROBE_POOL.submit(_run_probe, pid)
    return row


def _static_row(provider: str) -> dict | None:
    return next((row for row in pi_provider_map() if row["provider"] == provider), None)


def mapped_provider_rows() -> list[dict]:
    """Pi's provider map with verified availability merged in; a row still being probed reads
    'checking provider…' and is not selectable yet."""
    rows = pi_provider_map()
    if PROBE_TTL <= 0:
        return rows
    merged = []
    for row in rows:
        if not row["available"]:
            merged.append(row)
            continue
        state = _probe_state(row["provider"])
        merged.append({**row, **state} if state else {**row, "available": False, "error": "checking provider…"})
    return merged


def provider_model(provider: str) -> str:
    if provider == RANDOM_PROVIDER:
        return "any available"
    if provider == "local":
        return local_model()
    if provider == "openai-codex":
        return "gpt-6-luna"
    if provider in CORE_PROVIDERS:
        return DEEPSEEK_MODEL
    candidates = provider_candidates(provider)
    if not candidates:
        return ""
    state = _probe_state(provider)
    return str(state["model"]) if state and state.get("model") else str(candidates[0]["id"])


def provider_ids() -> tuple[str, ...]:
    """Every selectable condenser: the core backends plus Pi's mapped providers, in map order."""
    return CORE_PROVIDERS + tuple(row["provider"] for row in pi_provider_map())


def provider_available(provider: str) -> bool:
    """Selecting a provider needs a verified probe answer; with probing off the static
    credential + catalog check stands in. Restoring a saved selection uses `provider_ready`."""
    if provider == "deepseek" or provider == "local":
        return True
    if provider == "openai-codex":
        return bool(codex_discovery().get("available", False))
    if PROBE_TTL <= 0:
        row = _static_row(provider)
        return bool(row and row["available"])
    state = _probe_state(provider)
    return bool(state and state["available"])


def provider_ready(provider: str) -> bool:
    """A saved selection may be restored before any probe has run: mapped providers only need a
    credential, a cached catalog model and a chat-completions endpoint."""
    if provider in CORE_PROVIDERS:
        return provider_available(provider)
    row = _static_row(provider)
    return bool(row and row["available"])


def provider_choices() -> list[dict]:
    codex = codex_discovery()
    return [
        {"provider": "local", "label": "Local", "model": local_model(), "available": True, "error": ""},
        {"provider": "deepseek", "label": "DeepSeek", "model": DEEPSEEK_MODEL, "available": True, "error": ""},
        {"provider": "openai-codex", "label": "Codex Luna", "model": codex["model"], "available": bool(codex["available"]), "error": codex["error"]},
        *mapped_provider_rows(),
        {"provider": RANDOM_PROVIDER, "label": "Random", "model": "any available", "available": True, "error": ""},
    ]


def resolve_provider(provider: str) -> str:
    """A concrete condenser for one job; 'random' picks any available real provider on every call."""
    if provider != RANDOM_PROVIDER:
        return provider
    pool = [choice["provider"] for choice in provider_choices() if choice["available"] and choice["provider"] != RANDOM_PROVIDER]
    return random.choice(pool) if pool else "deepseek"


def resolve_snapshot(snapshot: dict) -> dict:
    """Snapshot with a real provider/model; an already-resolved snapshot passes through unchanged."""
    if snapshot.get("provider") != RANDOM_PROVIDER:
        return snapshot
    provider = resolve_provider(RANDOM_PROVIDER)
    return {**snapshot, "provider": provider, "model": provider_model(provider)}


def set_active_provider(provider: str) -> None:
    if provider not in provider_ids() and provider != RANDOM_PROVIDER:
        raise DebugInputError("unknown condenser provider")
    if provider != RANDOM_PROVIDER and not provider_available(provider):
        raise DebugInputError("condenser provider is unavailable")
    with PROVIDER_LOCK:
        global ACTIVE_PROVIDER
        ACTIVE_PROVIDER = provider


def user_settings(user_id: int = DEFAULT_USER_ID) -> dict:
    """One tenant's compaction settings. The default tenant's values are the live globals (loaded at startup,
    moved by /config), so a bare loopback client behaves exactly as it did before multi-tenancy; every other
    tenant reads its own config rows and falls back to the defaults a fresh install starts with."""
    if user_id == DEFAULT_USER_ID:
        with PROVIDER_LOCK:
            provider = ACTIVE_PROVIDER
        return {"tokens": int(BUDGET["tokens"]), "summary_words": summary_target(),
                "provider": provider, "set_at": SUMMARY["set_at"] or 0.0}
    rows: dict[str, tuple] = {}
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = {row[0]: (row[1], row[2]) for row in conn.execute(
                    "SELECT key, value, set_at FROM config WHERE user_id = ?", (int(user_id),))}
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("tenant settings", exc)
    try:
        tokens = int(rows["tokens"][0])
    except (KeyError, TypeError, ValueError):
        tokens = CONTEXT_TOKENS
    words_row = rows.get("summary_words")
    words = snap_summary_words(words_row[0]) if words_row else DEFAULT_SUMMARY_WORDS
    provider = str(rows.get("provider", (BOOT_PROVIDER, 0))[0])
    if provider not in provider_ids():
        provider = BOOT_PROVIDER
    return {"tokens": tokens, "summary_words": words, "provider": provider,
            "set_at": float(words_row[1]) if words_row else 0.0}


def condenser_snapshot(user_id: int = DEFAULT_USER_ID, provider: str | None = None) -> dict:
    """Immutable job snapshot for one tenant: its provider/model, its note length and the shared prompt."""
    settings = user_settings(user_id)
    selected = settings["provider"] if provider is None else provider
    return {"provider": selected, "model": provider_model(selected), "words": settings["summary_words"],
            "prompt": condenser_prompt(), "prompt_hash": condenser_prompt_hash(), "user": int(user_id)}
# Block compaction runs in background workers, not on the request thread: one job per (block, bucket), the
# request waits at most WAIT_MS, a still-running job is joined by the fallback slot at FALLBACK_MS, and the
# request never serves a pending marker — the worst case is the clipped transcript excerpt.
WORKERS = max(1, int(os.environ.get("PI_CONTEXT_ROUTER_WORKERS", "2")))
WAIT_MS = int(os.environ.get("PI_CONTEXT_ROUTER_WAIT_MS", "30000"))
FALLBACK_MS = int(os.environ.get("PI_CONTEXT_ROUTER_FALLBACK_MS", "20000"))
FALLBACK_PROVIDER = os.environ.get("PI_CONTEXT_ROUTER_FALLBACK_PROVIDER", "local")
FALLBACK_CHARS = int(os.environ.get("PI_CONTEXT_ROUTER_FALLBACK_CHARS", "1200"))
SUMMARY_POOL = futures.ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="ctx-condense")
SUMMARY_JOBS: dict[tuple, futures.Future] = {}
JOBS_LOCK = threading.Lock()


def set_tenant(user_id: int) -> None:
    """Attribute every counter, ledger row and config write that follows on this thread to one tenant."""
    TENANT.id = int(user_id)


def tenant_id() -> int:
    return getattr(TENANT, "id", DEFAULT_USER_ID)


def tenant_metrics(user_id: int | None = None) -> dict:
    uid = tenant_id() if user_id is None else int(user_id)
    if uid == DEFAULT_USER_ID:
        return METRICS
    bucket = TENANT_METRICS.get(uid)
    if bucket is None:
        bucket = dict(METRICS_ZERO)
        bucket["started"] = METRICS["started"]
        TENANT_METRICS[uid] = bucket
    return bucket


def _as_tenant(user_id: int, fn, *args):
    """Run one background job as the tenant whose request scheduled it."""
    set_tenant(user_id)
    return fn(*args)


def bump(**deltas: float) -> None:
    with METRICS_LOCK:
        bucket = tenant_metrics()
        for key, value in deltas.items():
            bucket[key] = bucket.get(key, 0) + value


WINDOW = 100
SERIES_HOURS = 24
REQUESTS_KEEP = int(os.environ.get("PI_CONTEXT_ROUTER_REQUESTS_KEEP", "2000"))


def record_request(entry: dict) -> None:
    """Append one router decision to ctx-engine.db; the dashboard window is read back from there."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                conn.execute(
                    "INSERT INTO requests (at, route, model, result, status, ms, raw_tokens, sent_tokens, provider, detail, raw, plan, cost, user_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry.get("at", time.time()), entry.get("route", ""), entry.get("model", ""), entry.get("result", ""),
                     entry.get("status"), entry.get("ms"), int(entry.get("raw_tokens") or 0), int(entry.get("sent_tokens") or 0),
                     entry.get("provider") or "deepseek", str(entry.get("detail") or "")[:2000], str(entry.get("raw") or "")[:4000],
                     str(entry.get("plan") or "")[:PLAN_LIMIT], compact_json(entry["cost"]) if entry.get("cost") else "",
                     int(entry.get("user_id") or tenant_id())),
                )
                conn.execute("DELETE FROM requests WHERE id < (SELECT MAX(id) FROM requests) - ?", (REQUESTS_KEEP,))
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("requests", exc)


def route_name(path: str) -> str:
    match = re.match(r"^/r/([a-z0-9_-]+)(/.*)$", path)
    return match.group(1) if match else ("codex" if path == "/codex/responses" else "?")


def model_of(body: bytes) -> str:
    try:
        value = json.loads(body).get("model")
        return value if isinstance(value, str) else ""
    except (json.JSONDecodeError, UnicodeDecodeError):
        return ""


# ----------------------------- summary cache (ctx-engine.db) -----------------------------

# One bucket per condenser backend: a DeepSeek summary and a local summary are different artefacts and
# must never answer for each other, so the cache key is (block digest, bucket, model, length target), not the
# digest alone.
BUCKETS = ("deepseek", "local", "openai-codex")
# One summary per (block digest, bucket, condenser model, word sizing hint). `tokens` is the historical
# column name; token-era rows are archived during migration before it is used for word-sized notes.
# length is persisted at once, so the cache holds a row per length instead of overwriting the previous one; the
# model is part of the identity too, so a condenser switch never serves a note written by the old model.
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS summaries (digest TEXT NOT NULL, provider TEXT NOT NULL, summary TEXT NOT NULL, "
    "model TEXT NOT NULL, tokens INTEGER NOT NULL DEFAULT 0, prompt_hash TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, "
    "PRIMARY KEY (digest, provider, model, tokens, prompt_hash))"
)
# A second domain in the same file: every router decision, so the dashboard window survives restarts. Rows are
# pruned to the last REQUESTS_KEEP on write; the display window (WINDOW) is deliberately smaller than retention.
REQUESTS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, route TEXT NOT NULL DEFAULT '', "
    "model TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '', status INTEGER, ms REAL, raw_tokens INTEGER NOT NULL DEFAULT 0, "
    "sent_tokens INTEGER NOT NULL DEFAULT 0, provider TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '', raw TEXT NOT NULL DEFAULT '', "
    "plan TEXT NOT NULL DEFAULT '', cost TEXT NOT NULL DEFAULT '', user_id INTEGER NOT NULL DEFAULT 1)"
)
# One row per tenant per key: the operator keeps the legacy rows (migrated to the default tenant id 1) and
# every other account gets its own compaction threshold, note length and condenser choice.
CONFIG_SCHEMA = ("CREATE TABLE IF NOT EXISTS config (user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                 "set_at REAL NOT NULL, PRIMARY KEY (user_id, key))")
# The exact text that already went on the wire for a block, one row per prompt identity. A real note (primary
# or fallback slot) is frozen on first serve; an excerpt is a deterministic floor and is never pinned, so the
# first note replaces it once and then never changes again — a block's wire text changes at most twice: clip to
# note, then fixed, and a later render never invalidates the provider's prompt cache.
PINS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS pins (digest TEXT NOT NULL, prompt_hash TEXT NOT NULL, summary TEXT NOT NULL, "
    "provider TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, PRIMARY KEY (digest, prompt_hash))"
)


def _archive_table(conn: sqlite3.Connection, source: str, base: str) -> str:
    """Rename `source` to a free `base`/`base_N` legacy name; returns the archive name. Never drops rows."""
    name = base
    suffix = 1
    while conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone():
        name = f"{base}_{suffix}"
        suffix += 1
    conn.execute(f'ALTER TABLE "{source}" RENAME TO "{name}"')
    return name


def _migrate_config_tenant(conn: sqlite3.Connection) -> None:
    """Move the pre-tenant config table onto (user_id, key); its rows belong to the default tenant.
    Rows are copied first and the original is archived under a legacy name — never dropped."""
    info = list(conn.execute("PRAGMA table_info(config)"))
    pk = {str(item[1]): int(item[5]) for item in info if int(item[5])}
    if pk == {"user_id": 1, "key": 2}:
        return
    columns = {str(item[1]) for item in info}
    conn.execute("DROP TABLE IF EXISTS config_tenant")
    conn.execute("CREATE TABLE config_tenant (user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
                 "set_at REAL NOT NULL, PRIMARY KEY (user_id, key))")
    if {"key", "value", "set_at"} <= columns:
        conn.execute("INSERT INTO config_tenant (user_id, key, value, set_at) SELECT 1, key, value, set_at FROM config")
    _archive_table(conn, "config", "config_legacy")
    conn.execute("ALTER TABLE config_tenant RENAME TO config")


def _migrate(conn: sqlite3.Connection) -> None:
    """Create or archive cache schemas without allowing stale prompt identities to answer."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='summaries'").fetchone()
    if row is None:
        conn.execute(SCHEMA)
        conn.execute(REQUESTS_SCHEMA)
        conn.execute(CONFIG_SCHEMA)
        conn.execute(PINS_SCHEMA)
        old_setting = conn.execute("SELECT value, set_at FROM config WHERE key = 'summary_tokens'").fetchone()
        if old_setting:
            conn.execute("INSERT OR IGNORE INTO config (user_id, key, value, set_at) VALUES (1, 'summary_tokens_tokens_legacy', ?, ?)", old_setting)
            conn.execute("DELETE FROM config WHERE key = 'summary_tokens'")
        conn.execute("INSERT OR IGNORE INTO config (user_id, key, value, set_at) VALUES (1, 'summary_length_unit', 'words', ?)", (time.time(),))
        conn.commit()
        return
    info = list(conn.execute("PRAGMA table_info(summaries)"))
    cols = {str(item[1]) for item in info}
    pk = [str(item[1]) for item in info if int(item[5])]
    expected_pk = ["digest", "provider", "model", "tokens", "prompt_hash"]
    if "words" in cols and "tokens" not in cols:
        _archive_table(conn, "summaries", "summaries_words_legacy")
        conn.execute(SCHEMA)
    elif pk != expected_pk or "prompt_hash" not in cols:
        _archive_table(conn, "summaries", "summaries_schema_legacy")
        conn.execute(SCHEMA)
        # Legacy rows do not carry the original condenser prompt, so assigning the current
        # prompt hash would falsely make them cache hits. Keep them in the archive only.
    conn.execute(REQUESTS_SCHEMA)
    conn.execute(CONFIG_SCHEMA)
    conn.execute(PINS_SCHEMA)
    request_cols = {str(item[1]) for item in conn.execute("PRAGMA table_info(requests)")}
    if "plan" not in request_cols:
        conn.execute("ALTER TABLE requests ADD COLUMN plan TEXT NOT NULL DEFAULT ''")
    if "cost" not in request_cols:
        conn.execute("ALTER TABLE requests ADD COLUMN cost TEXT NOT NULL DEFAULT ''")
    if "user_id" not in request_cols:
        # Existing traffic is the operator's own: it becomes the default tenant's ledger.
        conn.execute("ALTER TABLE requests ADD COLUMN user_id INTEGER NOT NULL DEFAULT 1")
    _migrate_config_tenant(conn)
    unit = conn.execute("SELECT value FROM config WHERE key = 'summary_length_unit'").fetchone()
    if unit is None:
        # Token-hint notes and persisted settings cannot be interpreted as word hints. Preserve both,
        # but archive the notes and never apply the old summary_tokens slider on restart.
        _archive_table(conn, "summaries", "summaries_tokens_legacy")
        conn.execute(SCHEMA)
        old_setting = conn.execute("SELECT value, set_at FROM config WHERE key = 'summary_tokens'").fetchone()
        if old_setting:
            conn.execute("INSERT OR IGNORE INTO config (user_id, key, value, set_at) VALUES (1, 'summary_tokens_tokens_legacy', ?, ?)", old_setting)
            conn.execute("DELETE FROM config WHERE key = 'summary_tokens'")
        conn.execute("INSERT INTO config (user_id, key, value, set_at) VALUES (1, 'summary_length_unit', 'words', ?)", (time.time(),))
    conn.commit()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    ensure_tenants(conn)
    _migrate(conn)
    return conn


def init_db() -> bool:
    """Create the cache database up front so a sandbox or permission problem is visible at startup."""
    try:
        with DB_LOCK:
            conn = db_connect()
            conn.close()
        return True
    except sqlite3.Error as exc:
        print(f"ctx-engine cache database unavailable ({type(exc).__name__}): summaries will not persist", file=sys.stderr, flush=True)
        return False


def cache_error(action: str, exc: sqlite3.Error) -> None:
    """One stderr line per minute; a broken cache must degrade to recomputing, never fail a turn."""
    now = time.time()
    if now - DB_ERROR["at"] > 60:
        DB_ERROR["at"] = now
        print(f"ctx-engine cache {action} failed ({type(exc).__name__}): {exc}", file=sys.stderr, flush=True)


# Persisted sliders: the values are strings in one key/value row each, written on every /config and applied
# over the env defaults at startup, so a restart keeps the chosen budget and block length.
CONFIG_KEYS = ("tokens", "summary_words", "provider")


def config_set_many(changes: dict[str, object], user_id: int = DEFAULT_USER_ID) -> bool:
    """Persist a validated group before callers mutate live state; one transaction, one tenant, fail-closed."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                for key, value in changes.items():
                    serialized = str(int(value)) if key in {"tokens", "summary_words"} else str(value)
                    conn.execute(
                        "INSERT INTO config (user_id, key, value, set_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, set_at=excluded.set_at",
                        (int(user_id), key, serialized, time.time()),
                    )
                conn.commit()
            finally:
                conn.close()
        return True
    except (sqlite3.Error, ValueError, TypeError) as exc:
        if isinstance(exc, sqlite3.Error):
            cache_error("config write", exc)
        return False


def config_set(key: str, value: object, user_id: int = DEFAULT_USER_ID) -> bool:
    return config_set_many({key: value}, user_id)


def load_config(user_id: int = DEFAULT_USER_ID) -> None:
    """Apply the default tenant's persisted sliders over the env defaults; a malformed row keeps its current value."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = {row[0]: (row[1], row[2]) for row in conn.execute(
                    "SELECT key, value, set_at FROM config WHERE user_id = ?", (int(user_id),))}
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("config read", exc)
        return
    try:
        if "tokens" in rows:
            BUDGET.update(tokens=int(rows["tokens"][0]), set_at=float(rows["tokens"][1]))
    except (TypeError, ValueError):
        pass
    try:
        if "summary_words" in rows:
            SUMMARY.update(words=snap_summary_words(rows["summary_words"][0]), set_at=float(rows["summary_words"][1]))
    except (TypeError, ValueError):
        pass
    saved_provider = rows.get("provider", ("deepseek", 0))[0]
    if saved_provider == RANDOM_PROVIDER or (saved_provider in provider_ids() and provider_ready(saved_provider)):
        with PROVIDER_LOCK:
            global ACTIVE_PROVIDER
            ACTIVE_PROVIDER = saved_provider
    prompt_row = rows.get("condenser_prompt")
    if prompt_row and isinstance(prompt_row[0], str) and prompt_row[0].strip() and len(prompt_row[0]) <= PROMPT_MAX_CHARS:
        with PROMPT_LOCK:
            global CONDENSER_PROMPT
            CONDENSER_PROMPT = prompt_row[0]


# ----------------------------- accounts & sessions -----------------------------
# One account per tenant. The default account is seeded on first use (its id is DEFAULT_USER_ID, which every
# legacy row already carries), dashboard sessions are cookie tokens, and each account also gets a long-lived
# api token for its Pi proxy path. Passwords are salted PBKDF2 hashes — never stored in the clear.
USERS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL COLLATE NOCASE UNIQUE, "
    "password TEXT NOT NULL, api_token TEXT NOT NULL UNIQUE, created_at REAL NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at REAL NOT NULL)",
)
SESSION_TTL_S = 30 * 24 * 3600
SESSION_COOKIE = "ctx_sess"
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
MIN_PASSWORD_CHARS = 6
PBKDF2_ROUNDS = 200_000
LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_S = 300.0
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
LOGIN_LOCK = threading.Lock()
TENANTS_READY = {"path": None}
AUTH_MISS = {"at": 0.0}


def ensure_tenants(conn: sqlite3.Connection) -> None:
    """Create the account tables and seed the operator's default tenant once per database file. No lock:
    two threads seeding at once are resolved by the UNIQUE username, and the loser's insert is ignored."""
    if TENANTS_READY["path"] == DB_PATH:
        return
    for statement in USERS_SCHEMA:
        conn.execute(statement)
    if not conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        try:
            _insert_user(conn, DEFAULT_USERNAME, DEFAULT_PASSWORD)
        except ValueError as exc:
            print(f"ctx-engine default tenant not seeded: {exc}", file=sys.stderr, flush=True)
    conn.commit()
    TENANTS_READY["path"] = DB_PATH


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def verify_password(stored: str, password: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = str(stored).split("$")
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError, AttributeError):
        return False
    return scheme == "pbkdf2_sha256" and hmac.compare_digest(digest.hex(), digest_hex)


def _insert_user(conn: sqlite3.Connection, username: str, password: str) -> dict:
    if not USERNAME_RE.match(str(username or "")):
        raise ValueError("username must be 3-32 characters: letters, digits, dot, dash or underscore")
    if len(str(password or "")) < MIN_PASSWORD_CHARS:
        raise ValueError(f"password must be at least {MIN_PASSWORD_CHARS} characters")
    token = secrets.token_urlsafe(24)
    try:
        cursor = conn.execute("INSERT INTO users (username, password, api_token, created_at) VALUES (?, ?, ?, ?)",
                              (username, hash_password(password), token, time.time()))
    except sqlite3.IntegrityError as exc:
        raise ValueError("username already taken") from exc
    return {"id": int(cursor.lastrowid), "username": username, "api_token": token}


def create_user(username: str, password: str) -> dict:
    with DB_LOCK:
        conn = db_connect()
        try:
            user = _insert_user(conn, username, password)
            conn.commit()
            return user
        finally:
            conn.close()


def _login_limited(username: str) -> bool:
    key = str(username or "").lower()
    now = time.monotonic()
    with LOGIN_LOCK:
        attempts = [stamp for stamp in LOGIN_ATTEMPTS.get(key, []) if now - stamp < LOGIN_WINDOW_S]
        if attempts:
            LOGIN_ATTEMPTS[key] = attempts
        else:
            LOGIN_ATTEMPTS.pop(key, None)
        if len(LOGIN_ATTEMPTS) > 1024:  # bound the map when bots spray unique usernames
            LOGIN_ATTEMPTS.clear()
        return len(attempts) >= LOGIN_MAX_ATTEMPTS


def _login_failed(username: str) -> None:
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.setdefault(str(username or "").lower(), []).append(time.monotonic())


def authenticate(username: str, password: str) -> dict | None:
    """One account by name and password, or None; ten failures in five minutes lock that name out."""
    if _login_limited(username):
        return None
    user = find_user(username)
    if user is None or not verify_password(user["password"], password):
        _login_failed(username)
        return None
    with LOGIN_LOCK:
        LOGIN_ATTEMPTS.pop(str(username or "").lower(), None)
    return {"id": user["id"], "username": user["username"], "api_token": user["api_token"]}


def _user_row(conn: sqlite3.Connection, where: str, value: object) -> dict | None:
    row = conn.execute(f"SELECT id, username, api_token FROM users WHERE {where} = ?", (value,)).fetchone()
    return {"id": int(row[0]), "username": str(row[1]), "api_token": str(row[2])} if row else None


def find_user(username: str) -> dict | None:
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute("SELECT id, username, api_token, password FROM users WHERE username = ?",
                                   (str(username or ""),)).fetchone()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("user read", exc)
        return None
    return dict(zip(("id", "username", "api_token", "password"), row)) if row else None


def user_by_id(user_id: int) -> dict | None:
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                return _user_row(conn, "id", int(user_id))
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("user read", exc)
        return None


def user_by_api_token(token: str) -> dict | None:
    if not token:
        return None
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                return _user_row(conn, "api_token", str(token))
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("token read", exc)
        raise


def default_user() -> dict | None:
    return user_by_id(DEFAULT_USER_ID)


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with DB_LOCK:
        conn = db_connect()
        try:
            conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)",
                         (token, int(user_id), time.time() + SESSION_TTL_S))
            conn.commit()
        finally:
            conn.close()
    return token


def session_user(token: str) -> dict | None:
    """The logged-in account for a session cookie, or None when it is unknown or expired. A database error
    is raised, never read as "no session": a locked cache must not kick a signed-in user back to the login."""
    if not token:
        return None
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute("SELECT u.id, u.username, u.api_token, s.expires_at FROM sessions s "
                                   "JOIN users u ON u.id = s.user_id WHERE s.token = ?", (str(token),)).fetchone()
                if row and float(row[3]) <= time.time():
                    conn.execute("DELETE FROM sessions WHERE token = ?", (str(token),))
                    conn.commit()
                    row = None
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("session read", exc)
        raise
    return {"id": int(row[0]), "username": str(row[1]), "api_token": str(row[2])} if row else None


def destroy_session(token: str) -> None:
    if not token:
        return
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                conn.execute("DELETE FROM sessions WHERE token = ?", (str(token),))
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("session drop", exc)


def cache_get(digest: str, provider: str, want_tokens: int | None = None, model: str | None = None, prompt_hash: str | None = None,
              count_miss: bool = True) -> str | None:
    """Read one note only for its provider, model, word bucket and prompt identity."""
    active_model = model or provider_model(provider)
    target = summary_target() if want_tokens is None else int(want_tokens)
    active_prompt_hash = prompt_hash or condenser_prompt_hash()
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute(
                    "SELECT summary FROM summaries WHERE digest = ? AND provider = ? AND model = ? AND tokens = ? AND prompt_hash = ?",
                    (digest, provider, active_model, target, active_prompt_hash),
                ).fetchone()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("read", exc)
        return None
    if row:
        return str(row[0])
    if count_miss:
        bump(cache_misses=1)
    return None


def cache_put(digest: str, summary: str, provider: str | None = None, tokens: int | None = None, model: str | None = None, prompt_hash: str | None = None) -> None:
    """One row per block, provider, model, word bucket and condenser prompt identity."""
    bucket = provider or "deepseek"
    target = summary_target() if tokens is None else int(tokens)
    active_model = model or provider_model(bucket)
    active_prompt_hash = prompt_hash or condenser_prompt_hash()
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO summaries (digest, provider, summary, model, tokens, prompt_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (digest, bucket, summary, active_model, target, active_prompt_hash, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("write", exc)


def pin_get(digest: str, prompt_hash: str) -> tuple[str, str] | None:
    """The exact text already sent for this block and prompt identity, with the slot that rendered it."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute("SELECT summary, provider FROM pins WHERE digest = ? AND prompt_hash = ?",
                                   (digest, prompt_hash)).fetchone()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("pin read", exc)
        return None
    return (str(row[0]), str(row[1])) if row else None


def pin_put(digest: str, prompt_hash: str, summary: str, provider: str) -> None:
    """The first real note wins forever. A legacy excerpt row (from before excerpts stopped being pinned) is
    replaced by that note; no later render can touch a real note."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                conn.execute(
                    "INSERT INTO pins (digest, prompt_hash, summary, provider, created_at) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(digest, prompt_hash) DO UPDATE SET summary=excluded.summary, provider=excluded.provider, "
                    "created_at=excluded.created_at WHERE pins.provider = 'excerpt'",
                    (digest, prompt_hash, summary, provider, time.time()))
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("pin write", exc)


def cache_counts() -> dict:
    """Per-bucket counts of live notes; legacy zero-length rows after the word migration are not counted."""
    counts = {name: 0 for name in BUCKETS}
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                for provider, total in conn.execute("SELECT provider, COUNT(*) FROM summaries WHERE tokens > 0 GROUP BY provider"):
                    counts[str(provider)] = int(total)
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("count", exc)
    return counts


def cache_count() -> int:
    return sum(cache_counts().values())


def recent_requests(limit: int = WINDOW, user_id: int = DEFAULT_USER_ID) -> list[dict]:
    """Newest-first router decisions for one tenant straight from ctx-engine.db, so restarts do not empty the window."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = conn.execute(
                    "SELECT id, at, route, model, result, status, ms, raw_tokens, sent_tokens, provider, detail, raw, cost "
                    "FROM requests WHERE user_id = ? ORDER BY id DESC LIMIT ?", (int(user_id), limit)
                ).fetchall()
            finally:
                conn.close()
        keys = ("id", "at", "route", "model", "result", "status", "ms", "raw_tokens", "sent_tokens", "provider", "detail", "raw", "cost")
        out = [dict(zip(keys, row)) for row in rows]
        for item in out:
            item["cost"] = json.loads(item["cost"]) if item["cost"] else None
        return out
    except sqlite3.Error as exc:
        cache_error("read", exc)
        return []


def request_detail(request_id: int, user_id: int = DEFAULT_USER_ID) -> dict | None:
    """One stored decision plus its parsed compaction plan, for the inspect modal; None when the row is gone
    or belongs to another tenant."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute(
                    "SELECT at, route, model, result, status, ms, raw_tokens, sent_tokens, provider, detail, plan, cost "
                    "FROM requests WHERE id = ? AND user_id = ?", (request_id, int(user_id))
                ).fetchone()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("detail", exc)
        return None
    if row is None:
        return None
    keys = ("at", "route", "model", "result", "status", "ms", "raw_tokens", "sent_tokens", "provider", "detail", "plan", "cost")
    detail = dict(zip(keys, row))
    detail["id"] = request_id
    for key in ("plan", "cost"):
        try:
            detail[key] = json.loads(detail[key]) if detail[key] else None
        except json.JSONDecodeError:
            detail[key] = None
    return detail


def _pct(values: list[float], q: float) -> float:
    """Nearest-rank percentile; a 100-record window does not justify interpolation."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return float(ordered[int(q * (len(ordered) - 1))])


def window_stats(rows: list[dict]) -> dict:
    """Rolling view over the newest decisions: rates, latency percentiles, token savings and breakdowns."""
    if not rows:
        return {"size": 0}
    span = max(0.0, rows[0]["at"] - rows[-1]["at"])
    lat = [float(r["ms"]) for r in rows if r["ms"] is not None]
    raw = sum(int(r["raw_tokens"] or 0) for r in rows)
    sent = sum(int(r["sent_tokens"] or 0) for r in rows)
    results = Counter(r["result"] for r in rows)
    errors = sum(n for name, n in results.items() if name not in {"passed", "compacted"})
    costs = [r.get("cost") or {} for r in rows]
    raw_usd = sum(float(c.get("raw_usd") or 0) for c in costs)
    upstream_usd = sum(float(c.get("sent_usd") or 0) for c in costs)
    condenser_usd = sum(float(c.get("condenser_usd") or 0) for c in costs)
    net_usd = sum(float(c.get("net_usd") or 0) for c in costs)
    return {
        "size": len(rows),
        "span_s": round(span, 1),
        "requests_per_min": round(60 * len(rows) / span, 1) if span else 0.0,
        "results": dict(results.most_common()),
        "pass_rate": round(100 * results["passed"] / len(rows), 1),
        "compact_rate": round(100 * results["compacted"] / len(rows), 1),
        "error_rate": round(100 * errors / len(rows), 1),
        "avg_ms": round(sum(lat) / len(lat), 1) if lat else 0.0,
        "p50_ms": round(_pct(lat, 0.5), 1),
        "p95_ms": round(_pct(lat, 0.95), 1),
        "max_ms": round(max(lat), 1) if lat else 0.0,
        "raw_tokens": raw,
        "sent_tokens": sent,
        "saved_tokens": max(0, raw - sent),
        "saved_percent": round(100 * (1 - sent / raw), 1) if raw else 0.0,
        "raw_usd": round(raw_usd, 6),
        "upstream_usd": round(upstream_usd, 6),
        "condenser_usd": round(condenser_usd, 6),
        "net_usd": round(net_usd, 6),
        "net_percent": round(100 * net_usd / raw_usd, 1) if raw_usd else 0.0,
        "routes": dict(Counter(r["route"] for r in rows).most_common()),
        "models": dict(Counter(r["model"] or "—" for r in rows).most_common()),
        "providers": dict(Counter(r["provider"] or "—" for r in rows).most_common()),
    }


def global_stats(user_id: int = DEFAULT_USER_ID) -> dict:
    """One tenant's whole retained ledger, so reclaimed context is a running total and not just the last-100 window."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute("SELECT COUNT(*), COALESCE(SUM(raw_tokens), 0), COALESCE(SUM(sent_tokens), 0), MIN(at), MAX(at) "
                                   "FROM requests WHERE user_id = ?", (int(user_id),)).fetchone()
                costs = [r[0] for r in conn.execute("SELECT cost FROM requests WHERE cost != '' AND user_id = ?", (int(user_id),)).fetchall()]
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("global", exc)
        return {"size": 0}
    raw, sent = int(row[1]), int(row[2])
    raw_usd = upstream_usd = net_usd = 0.0
    for text in costs:
        try:
            cost = json.loads(text)
        except json.JSONDecodeError:
            continue
        raw_usd += float(cost.get("raw_usd") or 0)
        upstream_usd += float(cost.get("sent_usd") or 0)
        net_usd += float(cost.get("net_usd") or 0)
    return {"size": int(row[0]), "raw_tokens": raw, "sent_tokens": sent, "saved_tokens": max(0, raw - sent),
            "saved_percent": round(100 * (1 - sent / raw), 1) if raw else 0.0, "raw_usd": round(raw_usd, 6),
            "upstream_usd": round(upstream_usd, 6), "net_usd": round(net_usd, 6),
            "net_percent": round(100 * net_usd / raw_usd, 1) if raw_usd else 0.0,
            "first_at": row[3], "last_at": row[4]}


def reclaimed_series(hours: int = SERIES_HOURS, user_id: int = DEFAULT_USER_ID) -> list[dict]:
    """Per-minute tokens reclaimed for one tenant over the last `hours`, the grid behind the dashboard chart."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = conn.execute(
                    "SELECT CAST(at / 60 AS INTEGER) AS minute, SUM(raw_tokens), SUM(sent_tokens), COUNT(*), GROUP_CONCAT(cost, char(31)) "
                    "FROM requests WHERE user_id = ? AND at >= ? GROUP BY minute ORDER BY minute",
                    (int(user_id), time.time() - hours * 3600)
                ).fetchall()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("series", exc)
        return []
    out = []
    for minute, raw, sent, count, costs in rows:
        net = 0.0
        for text in (costs or "").split("\x1f"):
            try:
                net += float(json.loads(text).get("net_usd") or 0)
            except (json.JSONDecodeError, AttributeError):
                continue
        out.append({"minute": int(minute), "raw_tokens": int(raw or 0), "sent_tokens": int(sent or 0),
                    "saved_tokens": max(0, int(raw or 0) - int(sent or 0)), "count": int(count), "net_usd": round(net, 6)})
    return out


def backfill_costs() -> int:
    """Fill `cost` for rows stored before cost accounting: raw_usd is exact from the stored token counts, sent_usd
    prices the stored sent tokens at the input rate. Cache hits and output were never captured, so the upstream
    figure is an upper bound and the net saving a lower bound; such rows carry `estimated: true`. Idempotent."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = conn.execute(
                    "SELECT id, model, raw_tokens, sent_tokens FROM requests WHERE cost = '' AND sent_tokens > 0 AND result IN ('compacted', 'passed')"
                ).fetchall()
                filled = 0
                for row_id, model, raw_tokens, sent_tokens in rows:
                    rates = model_rates(model)
                    if not rates:
                        continue
                    raw_usd = int(raw_tokens or 0) * rates.get("input", 0) / 1e6
                    sent_usd = int(sent_tokens or 0) * rates.get("input", 0) / 1e6
                    net = raw_usd - sent_usd
                    cost = {"model": model, "estimated": True, "raw_usd": round(raw_usd, 6), "sent_usd": round(sent_usd, 6),
                            "condenser_usd": 0.0, "net_usd": round(net, 6), "net_percent": round(100 * net / raw_usd, 1) if raw_usd else 0.0}
                    conn.execute("UPDATE requests SET cost = ? WHERE id = ?", (compact_json(cost), row_id))
                    filled += 1
                conn.commit()
            finally:
                conn.close()
        return filled
    except sqlite3.Error as exc:
        cache_error("backfill", exc)
        return 0


def reprice_costs() -> int:
    """Move stored `cost` rows onto the observed-cache baseline: raw tokens were first priced entirely at the input
    rate, which overstated the no-compaction world. Rows that recorded `in`/`cached`/`out` hold their own cache
    fraction, so they are rebuilt through `build_cost`; backfilled `estimated` rows never captured it and keep the
    old upper bound. Idempotent on the `raw_cached` key."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = conn.execute(
                    "SELECT id, model, raw_tokens, cost FROM requests WHERE cost != '' AND cost NOT LIKE '%\"raw_cached\"%'"
                ).fetchall()
                filled = 0
                for row_id, model, raw_tokens, text in rows:
                    try:
                        old = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if "in" not in old or "out" not in old:
                        continue
                    usage = {"prompt_tokens": int(old.get("in") or 0) + int(old.get("cached") or 0),
                             "prompt_cache_hit_tokens": int(old.get("cached") or 0),
                             "completion_tokens": int(old.get("out") or 0)}
                    condenser = {"calls": int(old.get("condenser_calls") or 0), "in": int(old.get("condenser_in") or 0),
                                 "out": int(old.get("condenser_out") or 0), "usd": float(old.get("condenser_usd") or 0.0)}
                    new = build_cost(model, int(raw_tokens or 0), usage, condenser)
                    if not new or "raw_usd" not in new:
                        continue
                    conn.execute("UPDATE requests SET cost = ? WHERE id = ?", (compact_json(new), row_id))
                    filled += 1
                conn.commit()
            finally:
                conn.close()
        return filled
    except sqlite3.Error as exc:
        cache_error("reprice", exc)
        return 0


def deepseek_key() -> str:
    with open(AUTH_PATH) as handle:
        entry = json.load(handle).get("deepseek") or {}
    key = entry.get("key") or entry.get("apiKey") or ""
    if not key:
        raise CondenserError("no deepseek credential in auth.json")
    return key


def condenser_text(payload: dict) -> str:
    """The answer text of one condenser response; empty when the model produced none."""
    choices = payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    return strip_thinking(str(message.get("content") or ""))


def condense_cost(payload: dict, provider: str = "deepseek", model: str | None = None) -> None:
    """Record only provider-reported usage; local text-only responses contribute no invented tokens."""
    if provider == "local":
        return
    usage = payload.get("usage") or {}
    if provider == "openai-codex":
        fresh = int(usage.get("input") or 0)
        cached = int(usage.get("cacheRead") or 0)
        out = int(usage.get("output") or 0)
    else:
        fresh, cached, out = billed_tokens(usage)
    actual_model = model or provider_model(provider)
    usd = cost_of(fresh, cached, out, model_rates(actual_model) or {})
    usage_add(fresh + cached, out, usd)
    bump(condenser_tokens_in=fresh + cached, condenser_tokens_out=out, condenser_cost_usd=usd)


def condenser_endpoint(provider: str, model: str) -> tuple[str, dict[str, str]]:
    """Chat URL and headers for the selected condenser; DeepSeek keeps its env-overridable endpoint."""
    if provider == "deepseek":
        return DEEPSEEK_URL, {"Content-Type": "application/json", "Authorization": f"Bearer {deepseek_key()}"}
    return provider_endpoint(provider, model)


def call_condenser(prompt: str, target: int | None = None, system_prompt: str | None = None,
                   provider: str | None = None, model: str | None = None) -> str:
    """Call exactly the snapshotted condenser; failures never fall back to another provider."""
    target = summary_target() if target is None else target
    provider = resolve_provider(ACTIVE_PROVIDER if provider is None else provider)
    model = provider_model(provider) if model is None else model
    system_prompt = condenser_prompt() if system_prompt is None else system_prompt
    hint = summary_hint(prompt, target)
    started = time.perf_counter()
    bump(**{f"condenser_calls_{provider}": 1})
    try:
        if provider in {"local", "openai-codex"}:
            result = _bridge_completion(provider, model, prompt + "\n\n" + hint, system_prompt)
            text, payload = result
            condense_cost(payload, provider, model)
            return normalize_summary(text)
        url, headers = condenser_endpoint(provider, model)
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": prompt + "\n\n" + hint}],
        }
        # DeepSeek's tuned fields stay DeepSeek-only: other vendors' reasoning models reject a custom
        # temperature or max_tokens outright, and the prompt already sizes the note.
        if provider == "deepseek":
            body["temperature"] = DEEPSEEK_TEMPERATURE
            body["max_tokens"] = DEEPSEEK_MAX_TOKENS
        payload = http_json(url, body, headers, CONDENSER_TIMEOUT)
        condense_cost(payload, provider, model)
        text = condenser_text(payload)
        if not text and provider == "deepseek":
            body["max_tokens"] = int(body.get("max_tokens") or DEEPSEEK_MAX_TOKENS) * 2
            payload = http_json(url, body, headers, CONDENSER_TIMEOUT)
            condense_cost(payload, provider, model)
            text = condenser_text(payload)
        if not text:
            raise CondenserError("condenser returned no usable text")
        return normalize_summary(text)
    finally:
        bump(condenser_ms_total=(time.perf_counter() - started) * 1000)


def _bridge_completion(provider: str, model: str, prompt: str, system_prompt: str) -> tuple[str, dict]:
    result = _bridge("complete-local" if provider == "local" else "complete-codex",
                     {"prompt": prompt, "system": system_prompt, "model": model,
                      "timeoutMs": int(CONDENSER_TIMEOUT * 1000)}, timeout=CONDENSER_TIMEOUT + 2)
    if not result.get("ok"):
        raise CondenserError("selected condenser failed")
    if result.get("provider") != provider or result.get("model") != model:
        raise CondenserError("selected condenser returned an unexpected model")
    text = str(result.get("text") or "").strip()
    if not text:
        raise CondenserError("condenser returned no usable text")
    return text, result


def summarize_block(block: list, tokens: int | None = None, model: str | None = None, system_prompt: str | None = None,
                   prompt_hash: str | None = None, provider: str | None = None) -> tuple[str, str]:
    """One summary per block, provider, model, length and prompt identity; never serve another snapshot's note."""
    ident = block_id(block)
    bucket = resolve_provider(ACTIVE_PROVIDER if provider is None else provider)
    model = provider_model(bucket) if model is None else model
    tokens = summary_target() if tokens is None else tokens
    system_prompt = condenser_prompt() if system_prompt is None else system_prompt
    prompt_hash = condenser_prompt_hash(system_prompt) if prompt_hash is None else prompt_hash
    prompt = condenser_input(block_transcript(block), verbatim_query(block))
    cached = cache_get(ident, bucket, tokens, model, prompt_hash)
    if cached and is_current_summary(cached):
        bump(cache_hits=1)
        return ident, cached
    try:
        signature = inspect.signature(call_condenser)
        parameters = signature.parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
    kwargs = {}
    if "system_prompt" in parameters or accepts_kwargs:
        kwargs["system_prompt"] = system_prompt
    if "provider" in parameters or accepts_kwargs:
        kwargs["provider"] = bucket
    if "model" in parameters or accepts_kwargs:
        kwargs["model"] = model
    summary = call_condenser(prompt, tokens, **kwargs)
    cache_put(ident, summary, bucket, tokens, model, prompt_hash)
    bump(blocks_summarized=1)
    return ident, summary


def _traced_summary(summary: str) -> str:
    # An excerpt is only a temporary floor: never stamp it as a finished condensation.
    return COMPACTED_TRACE + summary if is_current_summary(summary) and not summary.startswith(COMPACTED_TRACE) else summary


def summary_item(ident: str, summary: str) -> dict:
    return {"role": "user", "content": [{"type": "input_text", "text": _traced_summary(summary)}]}


def schedule_summary(block: list, summarize=summarize_block, snapshot: dict | None = None) -> futures.Future:
    """Start one background condensation for a complete provider/model/length/prompt identity."""
    snapshot = resolve_snapshot(condenser_snapshot() if snapshot is None else snapshot)
    user_id = int(snapshot.get("user") or DEFAULT_USER_ID)
    key = (block_id(block), snapshot["provider"], snapshot["model"], snapshot["words"], snapshot["prompt_hash"])
    with JOBS_LOCK:
        job = SUMMARY_JOBS.get(key)
        if job is None or job.done():
            if len(SUMMARY_JOBS) > 512:
                for old in [k for k, f in SUMMARY_JOBS.items() if f.done()]:
                    SUMMARY_JOBS.pop(old, None)
            job = SUMMARY_POOL.submit(
                _as_tenant, user_id, summarize, block, snapshot["words"], snapshot["model"], snapshot["prompt"],
                snapshot["prompt_hash"], snapshot["provider"]
            ) if summarize is summarize_block else SUMMARY_POOL.submit(_as_tenant, user_id, summarize, block)
            SUMMARY_JOBS[key] = job
    return job


def fallback_note(block: list) -> str:
    """The last-resort rendering: the block transcript clipped from the middle, never a pending marker."""
    text = " ".join(block_transcript(block).split())
    return clip_middle(text, FALLBACK_CHARS) if text else "(uncondensed)"


def pinned_note(digest: str, snapshot: dict) -> tuple[str, str] | None:
    """A served block answers with the exact text that already went on the wire: the pin first, then the
    selected slot, then the fallback slot. A rendering keeps the slot, model, word target and output length it
    was rendered with forever; only a new prompt identity starts a new rendering."""
    pinned = pin_get(digest, snapshot["prompt_hash"])
    if pinned is not None and pinned[1] != "excerpt":
        return pinned
    slots = [(snapshot["provider"], snapshot["model"])]
    if FALLBACK_PROVIDER != snapshot["provider"] and FALLBACK_PROVIDER in provider_ids():
        slots.append((FALLBACK_PROVIDER, provider_model(FALLBACK_PROVIDER)))
    for provider, model in slots:
        note = cache_get(digest, provider, snapshot["words"], model, snapshot["prompt_hash"], count_miss=False)
        if note and is_current_summary(note):
            return note, provider
    return None


def condense_completed(completed: list, summarize=summarize_block, snapshot: dict | None = None) -> list:
    """One note per completed block, computed in background workers. A block with a note — from the selected
    slot or the fallback slot — answers from cache at once; a cold block's primary job is awaited, joined at
    FALLBACK_MS by a fallback-slot job, and at WAIT_MS the request serves the primary note, the fallback note
    or the clipped transcript excerpt, never a pending marker. Every render is cached under its own slot,
    model, word target and prompt identity forever."""
    snapshot = condenser_snapshot() if snapshot is None else snapshot
    snapshots = [resolve_snapshot(snapshot) for _ in completed]
    marked = {index: compacted_note(block) for index, block in enumerate(completed)}
    pinned = {block_id(block): pinned_note(block_id(block), snapshots[index]) for index, block in enumerate(completed)
              if marked[index] is None}
    waiting = [(index, block) for index, block in enumerate(completed)
               if marked[index] is None and pinned[block_id(block)] is None]
    jobs = [schedule_summary(block, summarize, snapshots[index]) for index, block in waiting]
    deadline = time.monotonic() + WAIT_MS / 1000
    fallback_after = min(FALLBACK_MS, WAIT_MS * 2 / 3) / 1000
    if jobs:
        futures.wait(jobs, timeout=fallback_after)
    fallbacks = {}
    for (index, block), job in zip(waiting, jobs):
        if FALLBACK_PROVIDER not in provider_ids() or FALLBACK_PROVIDER == snapshots[index]["provider"] or (job.done() and job.exception() is None):
            continue
        fallback_slot = {**snapshots[index], "provider": FALLBACK_PROVIDER, "model": provider_model(FALLBACK_PROVIDER)}
        fallbacks[index] = schedule_summary(block, summarize, fallback_slot) if summarize is summarize_block \
            else SUMMARY_POOL.submit(_as_tenant, int(fallback_slot.get("user") or DEFAULT_USER_ID), summarize, block)
    if fallbacks:
        futures.wait(list(fallbacks.values()), timeout=max(0.0, deadline - time.monotonic()))
    authored = {index: job for (index, _), job in zip(waiting, jobs)}
    out = []
    reported = False
    for index, block in enumerate(completed):
        ident = block_id(block)
        if marked[index] is not None:
            out.append((ident, marked[index]))
            continue
        rendered = pinned[ident]
        if rendered is not None:
            note, provider = rendered
            bump(cache_hits=1)
            if provider not in ("pin", snapshots[index]["provider"]):
                bump(blocks_fallback=1)
            if provider != "pin":
                pin_put(ident, snapshot["prompt_hash"], note, provider)
        else:
            provider = snapshots[index]["provider"]
            job = authored[index]
            error = job.exception() if job.done() else None
            note = None
            if error is not None:
                bump(condenser_errors=1)
                if not reported:
                    print(f"ctx-engine condenser unavailable ({type(error).__name__}: {error}): serving the fallback note",
                          file=sys.stderr, flush=True)
                    reported = True
            elif job.done():
                note = job.result()[1] or None
            if note is None:
                provider = FALLBACK_PROVIDER
                fallback = fallbacks.get(index)
                if fallback is not None and fallback.done() and fallback.exception() is None:
                    note = fallback.result()[1] or None
                if note is None:
                    note = fallback_note(block)
                    provider = "excerpt"
                bump(blocks_fallback=1)
            if provider != "excerpt":
                pin_put(ident, snapshot["prompt_hash"], note, provider)
        out.append((ident, note))
    return out


def fit_live_turn(body: dict, key: str, token_limit: int) -> bool:
    """The hard budget: with nothing left to condense, the biggest text strings of the live turn are clipped
    from the middle until the token estimate fits. Image payloads are never touched and the request is never refused."""
    live = body[key]
    clipped = False
    while estimate_tokens(body) > token_limit:
        biggest = None
        for path, size in text_paths(live):
            if size > TRIM_FLOOR and (biggest is None or size > biggest[1]):
                biggest = (path, size)
        if biggest is None:
            break
        path, size = biggest
        path_assign(live, path, clip_middle(path_value(live, path), max(TRIM_FLOOR, size // 2)))
        clipped = True
    return clipped


def rewrite_chat(body: dict, token_limit: int, raw: bytes, report: dict | None = None, snapshot: dict | None = None) -> tuple[bytes, bool]:
    snapshot = condenser_snapshot() if snapshot is None else snapshot
    if report is not None:
        report.update(provider=snapshot["provider"], condenser_model=snapshot["model"])
    prefix, blocks = split_message_blocks(body["messages"])
    if not blocks:
        return raw, False
    live_items = [item for block in blocks[-LIVE_BLOCKS:] for item in block]
    completed = blocks[:-LIVE_BLOCKS]
    bump(blocks_seen=len(completed))
    fixed = {**body, "messages": prefix + live_items}
    if estimate_tokens(fixed) > token_limit:
        # The live turn alone is over budget: no summary can shrink it, so clip its biggest strings. Completed
        # blocks are condensed anyway so the request after this one finds them cached.
        if fit_live_turn(fixed, "messages", token_limit):
            bump(trimmed=1)
        if estimate_tokens(fixed) > token_limit:
            bump(overflow=1)
        for block in completed:
            if compacted_note(block) is None:
                schedule_summary(block, snapshot=snapshot)
        LAST_COMPACTION["at"] = time.time()
        encoded = compact_json(fixed).encode()
        bump(compacted=1, input_chars_sent=len(encoded))
        return encoded, True
    if not completed:
        bump(input_chars_sent=len(raw))
        return raw, False
    room = max(0, token_limit - estimate_tokens(fixed) - SUMMARY_ROOM_HEADROOM_TOKENS)
    summaries = condense_completed(completed, snapshot=snapshot)
    summaries = fit_summaries(summaries, room, chat_summary_item, lambda: bump(blocks_dropped=1))

    if report is not None:
        report["plan"] = compaction_plan(completed, summaries)
    rewritten = {**body, "messages": prefix + [chat_summary_item(i, t) for i, t in summaries] + live_items}
    encoded = compact_json(rewritten).encode()
    LAST_COMPACTION["at"] = time.time()
    bump(compacted=1, input_chars_sent=len(encoded))
    return encoded, True


def chat_summary_item(ident: str, summary: str) -> dict:
    return {"role": "user", "content": _traced_summary(summary)}


def rewrite_request(raw: bytes, token_limit: int | None = None, report: dict | None = None,
                    snapshot: dict | None = None) -> tuple[bytes, bool]:
    token_limit = BUDGET["tokens"] if token_limit is None else token_limit
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw, False
    if not isinstance(body, dict):
        return raw, False
    snapshot = condenser_snapshot() if snapshot is None else snapshot
    if report is not None:
        report.update(provider=snapshot["provider"], condenser_model=snapshot["model"])
    if has_image_content(body.get("messages") or []) or has_image_content(body.get("input") or []):
        bump(images=1)
    if isinstance(body.get("messages"), list) and body.get("input") is None:
        bump(input_chars_raw=len(raw))
        if estimate_tokens(body) <= token_limit:
            bump(input_chars_sent=len(raw))
            return raw, False
        return rewrite_chat(body, token_limit, raw, report, snapshot)
    if not isinstance(body.get("input"), list):
        return raw, False
    bump(input_chars_raw=len(raw))
    if estimate_tokens(body) <= token_limit:
        bump(input_chars_sent=len(raw))
        return raw, False
    blocks = split_blocks(body["input"])
    if not blocks:
        bump(input_chars_sent=len(raw))
        return raw, False
    live_items = [item for block in blocks[-LIVE_BLOCKS:] for item in block]
    completed = blocks[:-LIVE_BLOCKS]
    bump(blocks_seen=len(completed))
    fixed = {**body, "input": live_items}
    if estimate_tokens(fixed) > token_limit:
        # The live turn alone is over budget: no summary can shrink it, so clip its biggest strings. Completed
        # blocks are condensed anyway so the request after this one finds them cached.
        if fit_live_turn(fixed, "input", token_limit):
            bump(trimmed=1)
        if estimate_tokens(fixed) > token_limit:
            bump(overflow=1)
        for block in completed:
            if compacted_note(block) is None:
                schedule_summary(block, snapshot=snapshot)
        LAST_COMPACTION["at"] = time.time()
        encoded = compact_json(fixed).encode()
        bump(compacted=1, input_chars_sent=len(encoded))
        return encoded, True
    if not completed:
        bump(input_chars_sent=len(raw))
        return raw, False
    room = max(0, token_limit - estimate_tokens(fixed) - SUMMARY_ROOM_HEADROOM_TOKENS)
    summaries = condense_completed(completed, snapshot=snapshot)
    summaries = fit_summaries(summaries, room, summary_item, lambda: bump(blocks_dropped=1))

    if report is not None:
        report["plan"] = compaction_plan(completed, summaries)
    rewritten = {**body, "input": [summary_item(i, s) for i, s in summaries] + live_items}
    encoded = compact_json(rewritten).encode()
    LAST_COMPACTION["at"] = time.time()
    bump(compacted=1, input_chars_sent=len(encoded))
    return encoded, True


# Only the built UI is served; source files and the databases stay outside the web root.
FRONTEND_DIST = os.path.abspath(os.path.join(os.path.dirname(__file__), "../frontend/dist"))


# ----------------------------- session debugger -----------------------------

# These are the two persisted session stores used by Pi and SubagentX. Only these roots,
# their direct files, and one Pi project bucket level are eligible; arbitrary paths are never opened.
DEBUG_SESSION_ROOTS = (
    ("pi", os.path.realpath(os.path.expanduser("~/.pi/agent/sessions"))),
    ("subagentx", os.path.realpath(os.path.expanduser("~/.pi/agent/subagentx-sessions"))),
)
DEBUG_SESSION_MAX_BYTES = int(os.environ.get("PI_CONTEXT_ROUTER_DEBUG_MAX_SESSION_BYTES", str(64 * 1024 * 1024)))
DEBUG_SESSION_MAX_FILES = int(os.environ.get("PI_CONTEXT_ROUTER_DEBUG_MAX_SESSION_FILES", "10000"))
DEBUG_RESPONSE_MAX_BYTES = int(os.environ.get("PI_CONTEXT_ROUTER_DEBUG_MAX_RESPONSE_BYTES", str(64 * 1024 * 1024)))
DEBUG_PREVIEW_MAX_BODY_BYTES = 16 * 1024
DEBUG_LOCK = threading.RLock()
DEBUG_NOT_CONDENSED = "not yet condensed"


class DebugInputError(ValueError):
    pass


def _debug_project(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value or not os.path.isabs(value):
        raise DebugInputError("project must be an absolute path")
    return os.path.realpath(value)


def _debug_timestamp(value: object, fallback: float) -> float:
    try:
        if isinstance(value, (int, float)):
            number = float(value)
            return number / 1000 if number > 100_000_000_000 else number
        if isinstance(value, str):
            from datetime import datetime
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OverflowError):
        pass
    return fallback


def _debug_files() -> list[tuple[str, str, str]]:
    files: list[tuple[str, str, str]] = []
    for source, root in DEBUG_SESSION_ROOTS:
        try:
            entries = list(os.scandir(root))
        except OSError:
            continue
        for entry in entries:
            candidates = [entry]
            if entry.is_dir(follow_symlinks=False):
                try:
                    candidates.extend(os.scandir(entry.path))
                except OSError:
                    continue
            for candidate in candidates:
                if len(files) >= DEBUG_SESSION_MAX_FILES:
                    return files
                if not candidate.name.endswith(".jsonl") or not candidate.is_file(follow_symlinks=False):
                    continue
                try:
                    path = os.path.realpath(candidate.path)
                    if os.path.commonpath((root, path)) != root:
                        continue
                    if os.stat(path).st_size > DEBUG_SESSION_MAX_BYTES:
                        continue
                except OSError:
                    continue
                rel = os.path.relpath(path, root)
                files.append((source, root, rel))
    return files


def _session_wire_message(message: dict) -> dict:
    """Remove Pi journal metadata while retaining the message schema used by block_id/transcript."""
    allowed = {"role", "content", "name", "tool_calls", "tool_call_id", "function_call", "type", "id", "call_id", "arguments", "input", "output"}
    return {key: value for key, value in message.items() if key in allowed}


def _read_debug_session(source: str, root: str, relative: str) -> dict | None:
    path = os.path.realpath(os.path.join(root, relative))
    try:
        if os.path.commonpath((root, path)) != root or not path.endswith(".jsonl"):
            return None
        size = os.stat(path).st_size
        if size > DEBUG_SESSION_MAX_BYTES:
            raise DebugInputError("session file exceeds the configured size limit")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DebugInputError(f"session file unavailable: {type(exc).__name__}") from exc
    records = []
    for line in text.splitlines():
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    session = next((record for record in records if record.get("type") == "session"), {})
    project_raw = session.get("cwd")
    if not isinstance(project_raw, str) or not os.path.isabs(project_raw):
        return None
    project = os.path.realpath(project_raw)
    messages = []
    for record in records:
        message = record.get("message") if record.get("type") == "message" else None
        if isinstance(message, dict) and isinstance(message.get("role"), str):
            messages.append(_session_wire_message(message))
    _prefix, blocks = split_message_blocks(messages)
    session_id = session.get("id") if isinstance(session.get("id"), str) else os.path.splitext(os.path.basename(relative))[0]
    title = next((str(record.get("name")) for record in records if record.get("type") == "session_info" and record.get("name")), "")
    if not title:
        first_user = next((message for message in messages if message.get("role") == "user"), None)
        title = normalized_text(content_text(first_user.get("content")))[:120] if first_user else session_id
    try:
        fallback = os.stat(path).st_mtime
    except OSError:
        fallback = 0.0
    at = max((_debug_timestamp(record.get("timestamp"), fallback) for record in records), default=fallback)
    return {"id": f"{source}:{relative}", "title": title or session_id, "source": source, "at": at,
            "project": project, "blocks": blocks, "messages": messages, "path": path, "size": size}


def _debug_sessions(project: str | None = None) -> list[dict]:
    sessions = []
    with DEBUG_LOCK:
        for source, root, relative in _debug_files():
            session = _read_debug_session(source, root, relative)
            if session is not None and (project is None or session["project"] == project):
                sessions.append(session)
    sessions.sort(key=lambda session: (session["at"], session["id"]), reverse=True)
    return sessions


def debug_projects() -> dict:
    counts = Counter(session["project"] for session in _debug_sessions())
    return {"projects": [{"path": path, "sessions": counts[path]} for path in sorted(counts)]}


def debug_session_list(project: str) -> dict:
    return {"sessions": [{key: session[key] for key in ("id", "title", "source", "at")}
                         | {"blocks": len(session["blocks"])} for session in _debug_sessions(project)]}


def debug_session_detail(session_id: str, project: str) -> dict | None:
    sessions = _debug_sessions(project)
    session = next((item for item in sessions if item["id"] == session_id), None)
    if session is None:
        return None
    blocks = []
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                snapshot = condenser_snapshot()
                for block in session["blocks"]:
                    digest = block_id(block)
                    if snapshot["provider"] == RANDOM_PROVIDER:
                        row = conn.execute(
                            "SELECT summary FROM summaries WHERE digest = ? AND prompt_hash = ? "
                            "ORDER BY (tokens = ?) DESC, created_at DESC LIMIT 1",
                            (digest, snapshot["prompt_hash"], snapshot["words"]),
                        ).fetchone()
                    else:
                        row = conn.execute(
                            "SELECT summary FROM summaries WHERE digest = ? AND provider = ? AND model = ? AND prompt_hash = ? "
                            "ORDER BY (tokens = ?) DESC, created_at DESC LIMIT 1",
                            (digest, snapshot["provider"], snapshot["model"], snapshot["prompt_hash"], snapshot["words"]),
                        ).fetchone()
                    blocks.append({"id": digest, "before": block_transcript(block),
                                   "after": str(row[0]) if row else DEBUG_NOT_CONDENSED,
                                   "source": session["source"]})
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("debug read", exc)
        raise
    result = {"blocks": blocks, "id": session["id"], "title": session["title"], "source": session["source"], "at": session["at"]}
    encoded_size = len(compact_json(result).encode("utf-8"))
    if encoded_size > DEBUG_RESPONSE_MAX_BYTES:
        raise DebugInputError("debug session response exceeds the configured size limit")
    return result


def debug_preview(project: object, session_id: object, block_digest: object) -> dict:
    """Condense one real block selected from the allow-listed session stores.

    The request supplies identifiers only; the session file and condenser prompt are
    resolved from server-owned state, and summarize_block snapshots the prompt identity
    through the whole call/cache write.
    """
    project_path = _debug_project(project)
    if not isinstance(session_id, str) or not session_id or len(session_id) > 4096 or "\x00" in session_id:
        raise DebugInputError("id must be a non-empty session identifier")
    if not isinstance(block_digest, str) or not re.fullmatch(r"[0-9a-f]{16}", block_digest):
        raise DebugInputError("block_id must be a 16-character block digest")
    session = next((item for item in _debug_sessions(project_path) if item["id"] == session_id), None)
    if session is None:
        raise DebugInputError("session not found")
    block = next((item for item in session["blocks"] if block_id(item) == block_digest), None)
    if block is None:
        raise DebugInputError("block not found")
    _ident, summary = summarize_block(block)
    return {"after": summary}


def debug_prompt_payload() -> dict:
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute("SELECT value FROM config WHERE key = 'condenser_prompt'").fetchone()
            finally:
                conn.close()
    except sqlite3.Error:
        raise
    prompt = str(row[0]) if row and isinstance(row[0], str) and row[0].strip() else condenser_prompt()
    if row and isinstance(row[0], str) and row[0].strip() and len(row[0]) <= PROMPT_MAX_CHARS:
        with PROMPT_LOCK:
            global CONDENSER_PROMPT
            CONDENSER_PROMPT = row[0]
            prompt = row[0]
    return {"prompt": prompt, "default_prompt": DEFAULT_CONDENSER_PROMPT}


def save_debug_prompt(prompt: str) -> dict:
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > PROMPT_MAX_CHARS:
        raise DebugInputError(f"prompt must be a non-empty string of at most {PROMPT_MAX_CHARS} characters")
    if not config_set("condenser_prompt", prompt):
        raise sqlite3.OperationalError("prompt could not be persisted")
    with PROMPT_LOCK:
        global CONDENSER_PROMPT
        CONDENSER_PROMPT = prompt
    return {"prompt": prompt}



LOCALAI_DB_PATH = os.environ.get("LOCALAI_DB_PATH", os.path.expanduser("~/.pi/agent/localai.db"))


def token_stats() -> dict:
    """Read-only aggregates of local-model calls; never expose prompts, sessions or errors."""
    conn = sqlite3.connect(f"file:{LOCALAI_DB_PATH}?mode=ro", uri=True, timeout=2)
    try:
        conn.row_factory = sqlite3.Row
        fields = "count(*) requests, coalesce(sum(input_tokens),0) input_tokens, coalesce(sum(output_tokens),0) output_tokens, coalesce(sum(reasoning_tokens),0) reasoning_tokens, coalesce(sum(cached_tokens),0) cached_tokens, coalesce(sum(elapsed_ms),0) elapsed_ms"
        totals = dict(conn.execute(f"select {fields} from requests").fetchone())
        by_source = [dict(row) for row in conn.execute(f"select source, {fields} from requests group by source order by requests desc")]
        recent = [dict(row) for row in conn.execute("select seq, source, model, status, input_tokens, output_tokens, reasoning_tokens, cached_tokens, elapsed_ms, started_at / 1000.0 started from requests order by seq desc limit 100")]
        return {"now": time.time(), "totals": totals, "by_source": by_source, "recent": recent}
    finally:
        conn.close()


# ----------------------------- dashboard -----------------------------

def stats_snapshot(user_id: int = DEFAULT_USER_ID) -> dict:
    """One tenant's dashboard: its counters, its ledger window, its settings — plus the server-wide provider
    map and cache counters (the note cache is content-addressed, so it is shared by construction)."""
    with METRICS_LOCK:
        snapshot = dict(tenant_metrics(user_id))
    # Row `at` is stamped by this process's clock; the dashboard renders age and the 24h window against
    # `now` from the same clock, never the browser's, so a clock-skewed host cannot shift them.
    snapshot["now"] = time.time()
    recent = recent_requests(WINDOW, user_id)
    snapshot["recent"] = recent
    snapshot["window"] = window_stats(recent)
    snapshot["global"] = global_stats(user_id)
    snapshot["series"] = reclaimed_series(user_id=user_id)
    buckets = cache_counts()
    snapshot["cache_buckets"] = buckets
    snapshot["cache_entries"] = sum(buckets.values())
    snapshot["cache_db"] = DB_PATH
    calls = sum(snapshot.get(f"condenser_calls_{provider}", 0) for provider in provider_ids())
    snapshot["condenser_calls"] = calls
    snapshot["condenser_avg_ms"] = round(snapshot["condenser_ms_total"] / calls, 1) if calls else 0.0
    settings = user_settings(user_id)
    snapshot["provider"] = settings["provider"]
    snapshot["model"] = provider_model(settings["provider"])
    snapshot["providers"] = provider_choices()
    snapshot["token_limit"] = settings["tokens"]
    snapshot["summary_words"] = settings["summary_words"]
    snapshot["summary_word_steps"] = list(SUMMARY_WORD_STEPS)
    snapshot["summary_set_at"] = settings["set_at"] or None
    raw = snapshot["input_chars_raw"] or 0
    sent = snapshot["input_chars_sent"] or 0
    snapshot["input_tokens_raw_est"] = round(raw / CHARS_PER_TOKEN)
    snapshot["input_tokens_sent_est"] = round(sent / CHARS_PER_TOKEN)
    snapshot["saved_percent"] = round(100 * (1 - sent / raw), 1) if raw and sent else 0.0
    snapshot["uptime_s"] = round(time.time() - snapshot["started"], 1)
    snapshot["last_compaction"] = LAST_COMPACTION["at"] or None
    snapshot["chars_per_token"] = CHARS_PER_TOKEN
    snapshot["db_errors"] = 1 if DB_ERROR["at"] else 0
    snapshot["db_error_at"] = DB_ERROR["at"] or None
    account = user_by_id(user_id) or {}
    snapshot["user"] = {"username": str(account.get("username") or ""),
                        "api_token": str(account.get("api_token") or ""),
                        "operator": user_id == DEFAULT_USER_ID}
    return snapshot


def _debug_query(query: dict[str, list[str]], required: tuple[str, ...]) -> dict[str, str]:
    if set(query) != set(required) or any(len(query.get(key, [])) != 1 or not query[key][0] for key in required):
        raise DebugInputError("invalid query shape")
    return {key: query[key][0] for key in required}


class RouterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], upstream: str):
        super().__init__(address, RouterHandler)
        self.upstream = upstream


def connect_and_send(conn, method: str, target: str, body: bytes | None, headers: dict) -> None:
    """Send with one immediate retry: a DNS blip fails getaddrinfo before anything reaches upstream."""
    for attempt in (1, 2):
        try:
            conn.request(method, target, body=body, headers=headers)
            return
        except socket.gaierror:
            if attempt == 2:
                raise
            time.sleep(0.5)


class RouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "pi-context-router/2"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_bytes(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        for key, value in getattr(self, "_extra_headers", []):
            self.send_header(key, value)
        self._extra_headers = []
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    # ----------------------------- authentication -----------------------------
    def cookie_token(self) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return ""

    def principal(self) -> dict | None:
        """The account behind this call: session cookie first, then X-CTX-Token (a session or a tenant api
        token). PI_CONTEXT_ROUTER_REQUIRE_LOGIN=0 answers with the default tenant so the test suite and local
        tooling keep working without a login."""
        if not AUTH_ENABLED:
            return default_user()
        token = self.cookie_token() or self.headers.get("X-CTX-Token", "")
        return session_user(token) or user_by_api_token(token)

    def require_user(self) -> dict | None:
        """A logged-in account, or a 401/503 and None; every dashboard read and write runs as that tenant."""
        try:
            user = self.principal()
        except sqlite3.Error:
            self.send_bytes(503, b'{"error":"ctx-engine database unavailable"}')
            return None
        if user is None:
            self.send_bytes(401, b'{"error":"authentication required"}')
            self._log_auth_miss()
            return None
        set_tenant(user["id"])
        return user

    def _log_auth_miss(self) -> None:
        """One rate-limited line when a credential was presented but the store did not recognize it: an
        anonymous request is just the login screen, a rejected session is worth seeing in the journal."""
        if not (self.cookie_token() or self.headers.get("X-CTX-Token", "")):
            return
        now = time.time()
        if now - AUTH_MISS["at"] > 60:
            AUTH_MISS["at"] = now
            print("ctx-engine session not recognized (expired, logged out, or the account is gone)",
                  file=sys.stderr, flush=True)

    def require_operator(self) -> bool:
        """The debugger, local token journal and master condense are the operator's: another account gets a
        403, and an anonymous call is accepted only while the router keeps its loopback trust model."""
        try:
            user = self.principal()
        except sqlite3.Error:
            self.send_bytes(503, b'{"error":"ctx-engine database unavailable"}')
            return False
        if user is not None:
            if user["id"] != DEFAULT_USER_ID:
                self.send_bytes(403, b'{"error":"operator only"}')
                return False
            set_tenant(user["id"])
            return True
        if PUBLIC_DEPLOY:
            self.send_bytes(401, b'{"error":"authentication required"}')
            return False
        set_tenant(DEFAULT_USER_ID)
        return True

    def proxy_tenant(self) -> tuple[int | None, str]:
        """`/t/<api-token>/...` is that tenant's proxy path and works in any deployment. The bare paths stay
        the operator's own router until the deployment is marked public, which is when they need a token."""
        path = urlsplit(self.path).path.rstrip("/")
        match = re.match(r"^/t/([A-Za-z0-9_-]{8,})(/.*)$", path)
        if match:
            user = user_by_api_token(match.group(1))
            if user is None:
                return None, path
            set_tenant(user["id"])
            return user["id"], match.group(2)
        if PUBLIC_DEPLOY:
            return None, path
        set_tenant(DEFAULT_USER_ID)
        return DEFAULT_USER_ID, path

    def handle_account(self, path: str) -> None:
        """/api/login, /api/register and /api/logout: one JSON body in, a session cookie out."""
        if path == "/api/logout":
            # No body required: signing out must work from a bare `curl -X POST /api/logout`.
            destroy_session(self.cookie_token())
            self._extra_headers = [("Set-Cookie", f"{SESSION_COOKIE}=; Path=/; HttpOnly; Max-Age=0")]
            self.send_bytes(200, b'{"ok":true}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_bytes(400, b'{"error":"invalid content length"}')
            return
        if length <= 0 or length > 8 * 1024:
            self.send_bytes(413, b'{"error":"request body size rejected"}')
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.send_bytes(400, b'{"error":"invalid JSON"}')
            return
        if not isinstance(payload, dict):
            self.send_bytes(400, b'{"error":"invalid JSON"}')
            return
        username, password = payload.get("username"), payload.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            self.send_bytes(400, b'{"error":"username and password are required"}')
            return
        if path == "/api/login":
            user = authenticate(username, password)
            if user is None:
                self.send_bytes(401, b'{"error":"invalid username or password"}')
                return
        else:
            try:
                user = create_user(username, password)
            except ValueError as exc:
                self.send_bytes(400, compact_json({"error": str(exc)}).encode())
                return
            except sqlite3.Error:
                self.send_bytes(503, b'{"error":"ctx-engine database unavailable"}')
                return
        token = create_session(user["id"])
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto", "").lower() == "https" else ""
        self._extra_headers = [("Set-Cookie", f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; "
                                        f"Max-Age={SESSION_TTL_S}{secure}")]
        self.send_bytes(200, compact_json({"user": {**user, "operator": user["id"] == DEFAULT_USER_ID}}).encode())

    def serve_dashboard(self, name: str) -> None:
        path = os.path.join(FRONTEND_DIST, name)
        try:
            with open(path, "rb") as file:
                body = file.read()
        except OSError:
            self.send_bytes(503, b'{"error":"dashboard build unavailable"}')
            return
        self.send_bytes(200, body, mimetypes.guess_type(path)[0] or "application/octet-stream")

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path.startswith("/debug/") and not self.require_operator():
                return
            if path == "/debug/projects":
                if query:
                    raise DebugInputError("debug/projects does not accept query parameters")
                self.send_bytes(200, compact_json(debug_projects()).encode())
            elif path == "/debug/sessions":
                values = _debug_query(query, ("project",))
                self.send_bytes(200, compact_json(debug_session_list(_debug_project(values["project"]))).encode())
            elif path == "/debug/session":
                values = _debug_query(query, ("id", "project"))
                detail = debug_session_detail(values["id"], _debug_project(values["project"]))
                self.send_bytes(200 if detail else 404, compact_json(detail or {"error": "not found"}).encode())
            elif path == "/debug/prompt":
                if query:
                    raise DebugInputError("debug/prompt does not accept query parameters")
                self.send_bytes(200, compact_json(debug_prompt_payload()).encode())
            elif path == "/health":
                self.send_bytes(200, b'{"status":"ok"}')
            elif path == "/stats":
                user = self.require_user()
                if user is None:
                    return
                self.send_bytes(200, compact_json(stats_snapshot(user["id"])).encode())
            elif path in ("/dashboard", "/dashboard/index.html", "/dashboard/debugger"):
                self.serve_dashboard("index.html")
            elif path.startswith("/dashboard/assets/"):
                name = path.removeprefix("/dashboard/assets/")
                if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or ".." in name.split("/"):
                    self.send_bytes(404, b'{"error":"not found"}')
                    return
                self.serve_dashboard("assets/" + name)
            elif path == "/token-stats":
                if not self.require_operator():
                    return
                try:
                    self.send_bytes(200, compact_json(token_stats()).encode())
                except (sqlite3.Error, OSError) as exc:
                    print(f"ctx-engine token stats error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    self.send_bytes(503, b'{"error":"local token journal unavailable"}')
            elif path.startswith("/request/"):
                user = self.require_user()
                if user is None:
                    return
                detail = request_detail(int(path.rsplit("/", 1)[-1]), user["id"]) if path.rsplit("/", 1)[-1].isdigit() else None
                self.send_bytes(200 if detail else 404, compact_json(detail or {"error": "not found"}).encode())
            else:
                tenant, rest = self.proxy_tenant()
                if tenant is None:
                    self.send_bytes(401, b'{"error":"tenant token required"}')
                    return
                upstream, rest = self.resolve_upstream(rest)
                if upstream is None:
                    self.send_bytes(404, b'{"error":"not found"}')
                    return
                try:
                    self.forward_get(upstream, rest)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except Exception as exc:
                    print(f"ctx-engine GET error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    self.send_bytes(502, b'{"error":{"message":"context router upstream failure","type":"server_error"}}')
        except DebugInputError as exc:
            self.send_bytes(400, compact_json({"error": str(exc)}).encode())
        except sqlite3.Error as exc:
            cache_error("debug endpoint", exc)
            self.send_bytes(503, b'{"error":"ctx-engine database unavailable"}')

    def do_POST(self) -> None:
        started = time.perf_counter()
        parsed = urlsplit(self.path)
        path = parsed.path.rstrip("/")
        if path == "/debug/prompt" and parsed.query:
            self.send_bytes(400, b'{"error":"debug/prompt does not accept query parameters"}')
            return
        if path == "/debug/preview" and parsed.query:
            self.send_bytes(400, b'{"error":"debug/preview does not accept query parameters"}')
            return
        if path in ("/api/login", "/api/register", "/api/logout"):
            self.handle_account(path)
            return
        if (path == "/master/condense" or path.startswith("/debug/")) and not self.require_operator():
            return
        if path == "/master/condense":
            if parsed.query:
                self.send_bytes(400, b'{"error":"master/condense does not accept query parameters"}')
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                self.send_bytes(400, b'{"error":"invalid content length"}')
                return
            if length <= 0:
                self.send_bytes(400, b'{"error":"invalid request body size"}')
                return
            if length > 1024 * 1024:
                self.send_bytes(413, b'{"error":"request body too large"}')
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_bytes(400, b'{"error":"invalid JSON"}')
                return
            block = payload.get("block") if isinstance(payload, dict) and set(payload) == {"block"} else None
            if not isinstance(block, list) or not block or any(
                    not isinstance(item, dict) or not isinstance(item.get("role"), str) for item in block):
                self.send_bytes(400, b'{"error":"body must be {\\"block\\": [message objects]}"}')
                return
            ident = block_id(block)
            try:
                snapshot = resolve_snapshot(condenser_snapshot())
                note = compacted_note(block)
                if note is None:
                    _ident, summary = summarize_block(
                        block, snapshot["words"], snapshot["model"], snapshot["prompt"], snapshot["prompt_hash"], snapshot["provider"]
                    )
                    note = _traced_summary(summary)
            except (CondenserError, DebugInputError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                bump(condenser_errors=1)
                print(f"ctx-engine master condense failed ({type(exc).__name__})", file=sys.stderr, flush=True)
                self.send_bytes(502, b'{"error":"condenser unavailable"}')
                return
            self.send_bytes(200, compact_json({"id": ident, "note": note, "prompt_hash": snapshot["prompt_hash"]}).encode())
            return
        if path == "/debug/preview":
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                self.send_bytes(400, b'{"error":"invalid content length"}')
                return
            if length <= 0 or length > DEBUG_PREVIEW_MAX_BODY_BYTES:
                self.send_bytes(413, b'{"error":"preview request body size rejected"}')
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_bytes(400, b'{"error":"invalid JSON"}')
                return
            if not isinstance(payload, dict) or set(payload) != {"project", "id", "block_id"}:
                self.send_bytes(400, b'{"error":"body must be {\\"project\\": string, \\"id\\": string, \\"block_id\\": string}"}')
                return
            try:
                result = debug_preview(payload["project"], payload["id"], payload["block_id"])
            except DebugInputError as exc:
                self.send_bytes(400, compact_json({"error": str(exc)}).encode())
                return
            except sqlite3.Error:
                self.send_bytes(503, b'{"error":"ctx-engine database unavailable"}')
                return
            except (CondenserError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
                bump(condenser_errors=1)
                print(f"ctx-engine debug preview failed ({type(exc).__name__})", file=sys.stderr, flush=True)
                self.send_bytes(502, b'{"error":"debug preview condenser unavailable"}')
                return
            self.send_bytes(200, compact_json(result).encode())
            return
        if path == "/debug/prompt":
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                self.send_bytes(400, b'{"error":"invalid content length"}')
                return
            if length <= 0 or length > PROMPT_MAX_CHARS + 128:
                self.send_bytes(413, b'{"error":"prompt request body size rejected"}')
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_bytes(400, b'{"error":"invalid JSON"}')
                return
            if not isinstance(payload, dict) or set(payload) != {"prompt"}:
                self.send_bytes(400, b'{"error":"body must be {\\"prompt\\": string}"}')
                return
            try:
                result = save_debug_prompt(payload["prompt"])
            except DebugInputError as exc:
                self.send_bytes(400, compact_json({"error": str(exc)}).encode())
                return
            except sqlite3.Error:
                self.send_bytes(503, b'{"error":"ctx-engine database unavailable"}')
                return
            self.send_bytes(200, compact_json(result).encode())
            return
        if path == "/config":
            user = self.require_user()
            if user is None:
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self.send_bytes(400, b'{"error":"invalid JSON"}')
                return
            if not isinstance(payload, dict) or not payload or set(payload) - set(CONFIG_KEYS):
                self.send_bytes(400, b'{"error":"config contains an unknown key"}')
                return
            result: dict = {}
            if "tokens" in payload:
                try:
                    tokens = int(payload["tokens"])
                except (TypeError, ValueError):
                    tokens = 0
                if not 4096 <= tokens <= 4_000_000:
                    self.send_bytes(400, b'{"error":"tokens must be an integer between 4096 and 4000000"}')
                    return
                result["tokens"] = tokens
            if "summary_words" in payload:
                try:
                    summary_words = int(payload["summary_words"])
                except (TypeError, ValueError):
                    summary_words = 0
                if summary_words not in SUMMARY_WORD_STEPS:
                    allowed = ", ".join(str(step) for step in SUMMARY_WORD_STEPS)
                    self.send_bytes(400, compact_json({"error": f"summary_words must be one of {allowed}"}).encode())
                    return
                result["summary_words"] = summary_words
            if "provider" in payload:
                provider = payload["provider"]
                if not isinstance(provider, str) or (provider not in provider_ids() and provider != RANDOM_PROVIDER):
                    self.send_bytes(400, b'{"error":"unknown condenser provider"}')
                    return
                if provider != RANDOM_PROVIDER and not provider_available(provider):
                    self.send_bytes(409, b'{"error":"condenser provider is unavailable"}')
                    return
                result["provider"] = provider
            if not result or not config_set_many(result, user["id"]):
                self.send_bytes(503, b'{"error":"ctx-engine configuration could not be persisted"}')
                return
            if user["id"] == DEFAULT_USER_ID:
                # The default tenant's live settings are the legacy globals; every other tenant reads its
                # own rows on the next request, so there is nothing to push here.
                now = time.time()
                if "tokens" in result:
                    BUDGET.update(tokens=result["tokens"], set_at=now)
                if "summary_words" in result:
                    SUMMARY.update(words=result["summary_words"], set_at=now)
                if "provider" in result:
                    set_active_provider(result["provider"])
            self.send_bytes(200, compact_json(result).encode())
            return
        try:
            tenant, rest = self.proxy_tenant()
        except sqlite3.Error:
            self.send_bytes(503, b'{"error":"ctx-engine database unavailable"}')
            return
        if tenant is None:
            self.send_bytes(401, b'{"error":"tenant token required"}')
            return
        upstream, rest = self.resolve_upstream(rest)
        if upstream is None:
            self.send_bytes(404, b'{"error":"not found"}')
            return
        content_encoding = self.headers.get("Content-Encoding", "identity").lower()
        if content_encoding not in {"", "identity", "gzip", "zstd"}:
            self.send_bytes(415, compact_json({"error": f"unsupported request content encoding: {content_encoding}"}).encode())
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_bytes(400, b'{"error":"invalid content length"}')
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_bytes(413, b'{"error":"request body size rejected"}')
            return
        bump(requests=1)
        usage_start()
        raw = self.rfile.read(length)
        entry = None
        report: dict = {}
        try:
            decoded = gzip.decompress(raw) if content_encoding == "gzip" else zstd.decompress(raw) if content_encoding == "zstd" else raw
            settings = user_settings(tenant)
            body, compacted = rewrite_request(decoded, token_limit=settings["tokens"], report=report,
                                              snapshot=condenser_snapshot(tenant, settings["provider"]))
            if not compacted:
                bump(passed=1)
            entry = {"at": time.time(), "route": route_name(path), "model": model_of(body),
                     "raw_tokens": estimate_tokens(decoded), "sent_tokens": estimate_tokens(body),
                     "result": "compacted" if compacted else "passed", "status": None, "ms": None, "detail": "",
                     "raw": decoded[:8000].decode("utf-8", "replace"), "plan": report.get("plan", ""),
                     "provider": report.get("provider", ACTIVE_PROVIDER)}
            if content_encoding == "gzip":
                body = gzip.compress(body, mtime=0) if compacted else raw
            elif content_encoding == "zstd":
                body = zstd.compress(body) if compacted else raw
        except (gzip.BadGzipFile, zstd.ZstdError, EOFError):
            self.send_bytes(400, b'{"error":"invalid compressed request body"}')
            record_request({"at": time.time(), "route": route_name(path), "model": "", "raw_tokens": estimate_tokens(raw),
                            "sent_tokens": 0, "result": "bad-body", "status": 400, "ms": round((time.perf_counter() - started) * 1000, 1), "detail": ""})
            return
        try:
            self.forward(body, compacted, upstream, rest, entry)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
            entry["result"] = "client-abort"
        except Exception as exc:
            print(f"ctx-engine upstream error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            entry["result"] = "upstream-error"
            entry["detail"] = type(exc).__name__
            try:
                self.send_bytes(502, b'{"error":{"message":"context router upstream failure","type":"server_error"}}')
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
        entry["cost"] = build_cost(entry["model"], entry["raw_tokens"], entry.pop("usage", {}), usage_snapshot())
        entry["ms"] = round((time.perf_counter() - started) * 1000, 1)
        record_request(entry)

    def resolve_upstream(self, path: str) -> tuple[str | None, str]:
        """`/codex/responses` -> codex upstream; `/r/<provider>/...` -> that provider's backend."""
        if path == "/codex/responses":
            return self.server.upstream, path
        match = re.match(r"^/r/([a-z0-9_-]+)(/.*)$", path)
        if match and match.group(1) in ROUTES:
            return ROUTES[match.group(1)], match.group(2)
        return None, path

    def forward_get(self, upstream_url: str, rest: str) -> None:
        upstream = urlsplit(upstream_url)
        connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": 120}
        if upstream.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        conn = connection_cls(upstream.hostname, upstream.port, **kwargs)
        query = ("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_HEADERS | {"host", "accept-encoding"}}
        headers["Host"] = upstream.netloc
        try:
            connect_and_send(conn, "GET", f"{upstream.path.rstrip('/')}{rest}{query}", None, headers)
            response = conn.getresponse()
            body = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS | {"content-length"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        finally:
            conn.close()
            self.close_connection = True

    def forward(self, body: bytes, compacted: bool, upstream_url: str, rest: str, entry: dict | None = None) -> None:
        upstream = urlsplit(upstream_url)
        if upstream.scheme not in {"http", "https"} or not upstream.hostname:
            raise ValueError("invalid upstream URL")
        connection_cls = http.client.HTTPSConnection if upstream.scheme == "https" else http.client.HTTPConnection
        kwargs = {"timeout": 600}
        if upstream.scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        conn = connection_cls(upstream.hostname, upstream.port, **kwargs)
        query = ("?" + self.path.split("?", 1)[1]) if "?" in self.path else ""
        target = f"{upstream.path.rstrip('/')}{rest}{query}"
        headers = {key: value for key, value in self.headers.items() if key.lower() not in HOP_HEADERS | {"host", "content-length", "accept-encoding"}}
        headers["Content-Length"] = str(len(body))
        headers["Host"] = upstream.netloc
        try:
            connect_and_send(conn, "POST", target, body, headers)
            response = conn.getresponse()
            if entry is not None:
                entry["status"] = response.status
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in HOP_HEADERS | {"content-length"}:
                    self.send_header(key, value)
            if response.getheader("Content-Length"):
                self.send_header("Content-Length", response.getheader("Content-Length"))
            self.send_header("X-Context-Router", "compacted" if compacted else "pass")
            self.send_header("Connection", "close")
            self.end_headers()
            tail = bytearray()
            while chunk := response.read(64 * 1024):
                self.wfile.write(chunk)
                self.wfile.flush()
                tail += chunk
                if len(tail) > TAIL_KEEP:
                    del tail[:-TAIL_KEEP]
            if entry is not None:
                entry["usage"] = usage_from_tail(bytes(tail))
        finally:
            conn.close()
            self.close_connection = True


def make_server(host: str = HOST, port: int = PORT, upstream: str = UPSTREAM) -> RouterServer:
    return RouterServer((host, port), upstream)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--upstream", default=UPSTREAM)
    args = parser.parse_args()
    if args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise SystemExit("ctx-engine server must bind loopback")
    server = make_server(args.host, args.port, args.upstream)
    cache_state = "ready" if init_db() else "unavailable"
    if cache_state == "ready":
        load_config()
        backfill_costs()
        reprice_costs()
    print(f"ctx-engine server listening on {args.host}:{server.server_port} (condenser {DEEPSEEK_MODEL}, {BUDGET['tokens']} tokens, cache {cache_state})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()