#!/usr/bin/env python3
"""Loopback-only context router: provider proxy + settled-block condenser with a dashboard.

Two jobs, both outside Pi so the transcript is never touched:

1. **Proxy.** `/codex/responses` (and opt-in `/r/<provider>/...`) is forwarded upstream with the caller's
   authorization intact, streaming preserved, request encodings (identity/gzip/zstd) supported, images passed
   through untouched and never logged.
2. **Condense.** Above `PI_CONTEXT_ROUTER_TOKENS` estimated tokens (text only — image payloads are never
   measured), every *completed* turn block (a block
   runs from one user message to just before the next) is replaced by a one-line cached summary, while the
   current turn (last user message onward) is passed through byte-exact, and a live turn too large for the
   budget has its own biggest text strings clipped from the middle rather than being refused or forwarded whole.
   Block identity is the SHA-256 of
   the canonical block JSON, so a summary is computed once per distinct block and then served from `ctx-engine.db`
   (SQLite, WAL): the cache survives restarts and is only extended, never cleared implicitly.

The condenser runs the DeepSeek Flash backend: the prompt-policy block that beat hard thinking
budgets in the benchmark (OODA/Pareto/MECE/BLUF + hard rules), a structured note under Task/Query / Actions Performed / Conclusion,
and temperature 0.1 (a reasoning model that over-thinks as it warms). Counters are visible at `/dashboard`
and `/stats`.
"""

from __future__ import annotations

from collections import Counter

import argparse
import gzip
import hashlib
import http.client
import inspect
import compression.zstd as zstd
import json
import mimetypes
import os
import re
import socket
import sqlite3
import ssl
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
    "blocks_seen": 0, "blocks_summarized": 0, "blocks_pending": 0, "blocks_dropped": 0, "cache_hits": 0,
    "condenser_errors": 0, "condenser_calls_deepseek": 0, "condenser_ms_total": 0.0,
    "condenser_tokens_in": 0, "condenser_tokens_out": 0, "condenser_cost_usd": 0.0,
    "input_chars_raw": 0, "input_chars_sent": 0, "started": time.time(),
}
LAST_COMPACTION = {"at": 0.0}
METRICS_LOCK = threading.Lock()
REQUEST_USAGE = threading.local()
DB_LOCK = threading.Lock()
DB_ERROR = {"at": 0.0}
# The compaction threshold is read at request time, never frozen into a signature, so the dashboard can move it
# without a restart; a restart falls back to PI_CONTEXT_ROUTER_TOKENS.
BUDGET = {"tokens": CONTEXT_TOKENS, "set_at": 0.0}
# Block compaction runs in background workers, not on the request thread: one job per (block, bucket), the
# request waits at most WAIT_MS for those jobs, and anything still running is cached for the next request.
WORKERS = max(1, int(os.environ.get("PI_CONTEXT_ROUTER_WORKERS", "2")))
WAIT_MS = int(os.environ.get("PI_CONTEXT_ROUTER_WAIT_MS", "2000"))
SUMMARY_POOL = futures.ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="ctx-condense")
SUMMARY_JOBS: dict[tuple, futures.Future] = {}
JOBS_LOCK = threading.Lock()


def bump(**deltas: float) -> None:
    with METRICS_LOCK:
        for key, value in deltas.items():
            METRICS[key] = METRICS.get(key, 0) + value


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
                    "INSERT INTO requests (at, route, model, result, status, ms, raw_tokens, sent_tokens, provider, detail, raw, plan, cost) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (entry.get("at", time.time()), entry.get("route", ""), entry.get("model", ""), entry.get("result", ""),
                     entry.get("status"), entry.get("ms"), int(entry.get("raw_tokens") or 0), int(entry.get("sent_tokens") or 0),
                     "deepseek", str(entry.get("detail") or "")[:2000], str(entry.get("raw") or "")[:4000],
                     str(entry.get("plan") or "")[:PLAN_LIMIT], compact_json(entry["cost"]) if entry.get("cost") else ""),
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
BUCKETS = ("deepseek",)
# One summary per (block digest, bucket, condenser model, note length). The note length is global now, but every
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
    "plan TEXT NOT NULL DEFAULT '', cost TEXT NOT NULL DEFAULT '')"
)
# The two dashboard sliders persist here, one row per key, so a restart keeps the chosen threshold instead of
# reverting to the env default. The DB is already the server's writable state store.
CONFIG_SCHEMA = "CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL, set_at REAL NOT NULL)"


def _archive_table(conn: sqlite3.Connection, source: str, base: str) -> str:
    """Rename `source` to a free `base`/`base_N` legacy name; returns the archive name. Never drops rows."""
    name = base
    suffix = 1
    while conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?", (name,)).fetchone():
        name = f"{base}_{suffix}"
        suffix += 1
    conn.execute(f'ALTER TABLE "{source}" RENAME TO "{name}"')
    return name


def _migrate(conn: sqlite3.Connection) -> None:
    """Create or archive cache schemas without allowing stale prompt identities to answer."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='summaries'").fetchone()
    if row is None:
        conn.execute(SCHEMA)
        conn.execute(REQUESTS_SCHEMA)
        conn.execute(CONFIG_SCHEMA)
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
        legacy = _archive_table(conn, "summaries", "summaries_schema_legacy")
        conn.execute(SCHEMA)
        # Legacy rows do not carry the original condenser prompt, so assigning the current
        # prompt hash would falsely make them cache hits. Keep them in the archive only;
        # a subsequent request re-condenses under the known prompt identity.
    conn.execute(REQUESTS_SCHEMA)
    conn.execute(CONFIG_SCHEMA)
    request_cols = {str(item[1]) for item in conn.execute("PRAGMA table_info(requests)")}
    if "plan" not in request_cols:
        conn.execute("ALTER TABLE requests ADD COLUMN plan TEXT NOT NULL DEFAULT ''")
    if "cost" not in request_cols:
        conn.execute("ALTER TABLE requests ADD COLUMN cost TEXT NOT NULL DEFAULT ''")
    conn.commit()


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=5)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
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
CONFIG_KEYS = ("tokens", "summary_tokens")


def config_set(key: str, value: object) -> bool:
    """Persist a config value and report failure to callers that need fail-closed writes."""
    try:
        serialized = str(int(value)) if key in {"tokens", "summary_tokens"} else str(value)
        with DB_LOCK:
            conn = db_connect()
            try:
                conn.execute(
                    "INSERT INTO config (key, value, set_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, set_at=excluded.set_at",
                    (key, serialized, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except (sqlite3.Error, ValueError, TypeError) as exc:
        if isinstance(exc, sqlite3.Error):
            cache_error(f"config write {key}", exc)
        return False


def load_config() -> None:
    """Apply persisted sliders over the env defaults; a malformed row falls back to the current value."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = {row[0]: (row[1], row[2]) for row in conn.execute("SELECT key, value, set_at FROM config")}
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
        if "summary_tokens" in rows:
            SUMMARY.update(tokens=snap_summary_tokens(rows["summary_tokens"][0]), set_at=float(rows["summary_tokens"][1]))
    except (TypeError, ValueError):
        pass
    prompt_row = rows.get("condenser_prompt")
    if prompt_row and isinstance(prompt_row[0], str) and prompt_row[0].strip() and len(prompt_row[0]) <= PROMPT_MAX_CHARS:
        with PROMPT_LOCK:
            global CONDENSER_PROMPT
            CONDENSER_PROMPT = prompt_row[0]


def cache_get(digest: str, provider: str, want_tokens: int | None = None, model: str | None = None, prompt_hash: str | None = None) -> str | None:
    """Read one row for the named bucket at the requested length and currently selected condenser model.

    Lengths coexist: an explicit `want_tokens` filters on `tokens`, so a note written at one length never answers
    another; when `want_tokens` is omitted the current global length is used. A row written by a
    different model is a miss too, so a condenser switch recomputes with the active model and persists that row
    alongside the old one.
    """
    active_model = model or DEEPSEEK_MODEL
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
    bump(cache_misses=1)
    return None


def cache_put(digest: str, summary: str, provider: str | None = None, tokens: int | None = None, model: str | None = None, prompt_hash: str | None = None) -> None:
    """One row per block, bucket, model, length and condenser prompt identity."""
    bucket = provider or "deepseek"
    tokens = summary_target() if tokens is None else int(tokens)
    active_model = model or DEEPSEEK_MODEL
    active_prompt_hash = prompt_hash or condenser_prompt_hash()
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO summaries (digest, provider, summary, model, tokens, prompt_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (digest, bucket, summary, active_model, int(tokens or 0), active_prompt_hash, time.time()),
                )
                conn.commit()
            finally:
                conn.close()
    except sqlite3.Error as exc:
        cache_error("write", exc)


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


def recent_requests(limit: int = WINDOW) -> list[dict]:
    """Newest-first router decisions straight from ctx-engine.db, so restarts do not empty the window."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = conn.execute(
                    "SELECT id, at, route, model, result, status, ms, raw_tokens, sent_tokens, provider, detail, raw, cost "
                    "FROM requests ORDER BY id DESC LIMIT ?", (limit,)
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


def request_detail(request_id: int) -> dict | None:
    """One stored decision plus its parsed compaction plan, for the inspect modal; None when the row is gone."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute(
                    "SELECT at, route, model, result, status, ms, raw_tokens, sent_tokens, provider, detail, plan, cost "
                    "FROM requests WHERE id = ?", (request_id,)
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


def global_stats() -> dict:
    """Whole retained ledger, so reclaimed context is a running total and not just the last-100 window."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                row = conn.execute("SELECT COUNT(*), COALESCE(SUM(raw_tokens), 0), COALESCE(SUM(sent_tokens), 0), MIN(at), MAX(at) FROM requests").fetchone()
                costs = [r[0] for r in conn.execute("SELECT cost FROM requests WHERE cost != ''").fetchall()]
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


def reclaimed_series(hours: int = SERIES_HOURS) -> list[dict]:
    """Per-minute tokens reclaimed over the last `hours`, the grid behind the dashboard chart."""
    try:
        with DB_LOCK:
            conn = db_connect()
            try:
                rows = conn.execute(
                    "SELECT CAST(at / 60 AS INTEGER) AS minute, SUM(raw_tokens), SUM(sent_tokens), COUNT(*), GROUP_CONCAT(cost, char(31)) "
                    "FROM requests WHERE at >= ? GROUP BY minute ORDER BY minute", (time.time() - hours * 3600,)
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


def condense_cost(payload: dict) -> None:
    """Charge one condenser response. Background jobs outlive the request that needed them, so every call also
    lands in a running total; the per-request accumulator only sees calls made on the request's own thread."""
    fresh, cached, out = billed_tokens(payload.get("usage") or {})
    usd = cost_of(fresh, cached, out, model_rates(DEEPSEEK_MODEL) or {})
    usage_add(fresh + cached, out, usd)
    bump(condenser_tokens_in=fresh + cached, condenser_tokens_out=out, condenser_cost_usd=usd)


def call_condenser(prompt: str, target: int | None = None, system_prompt: str | None = None) -> str:
    """One condense call through DeepSeek. Never logs the prompt or any credential."""
    target = summary_target(prompt) if target is None else target
    hint = summary_hint(prompt, target)
    body = {
        "model": DEEPSEEK_MODEL,
        "messages": [{"role": "system", "content": condenser_prompt() if system_prompt is None else system_prompt}, {"role": "user", "content": prompt + "\n\n" + hint}],
        "temperature": DEEPSEEK_TEMPERATURE,
        "stream": False,
        "max_tokens": DEEPSEEK_MAX_TOKENS,
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {deepseek_key()}"}
    bump(condenser_calls_deepseek=1)
    started = time.perf_counter()
    try:
        payload = http_json(DEEPSEEK_URL, body, headers, CONDENSER_TIMEOUT)
        condense_cost(payload)
        text = condenser_text(payload)
        if not text:
            # DeepSeek is a reasoning model: a small budget can be spent entirely on reasoning, leaving
            # `content` empty. One bounded retry with room for both, then fail open.
            body["max_tokens"] = int(body.get("max_tokens") or DEEPSEEK_MAX_TOKENS) * 2
            payload = http_json(DEEPSEEK_URL, body, headers, CONDENSER_TIMEOUT)
            condense_cost(payload)
            text = condenser_text(payload)
    finally:
        bump(condenser_ms_total=(time.perf_counter() - started) * 1000)
    if not text:
        raise CondenserError("condenser returned no usable text")
    return normalize_summary(text)


def summarize_block(block: list, tokens: int | None = None, model: str | None = None, system_prompt: str | None = None, prompt_hash: str | None = None) -> tuple[str, str]:
    """One summary per block, model, length and prompt identity; never serve another prompt's note."""
    ident = block_id(block)
    bucket = "deepseek"
    model = DEEPSEEK_MODEL if model is None else model
    tokens = summary_target() if tokens is None else tokens
    system_prompt = condenser_prompt() if system_prompt is None else system_prompt
    prompt_hash = condenser_prompt_hash(system_prompt) if prompt_hash is None else prompt_hash
    transcript = block_transcript(block)
    cached = cache_get(ident, bucket, tokens, model, prompt_hash)
    if cached and is_current_summary(cached):
        bump(cache_hits=1)
        return ident, cached
    try:
        signature = inspect.signature(call_condenser)
    except (TypeError, ValueError):
        accepts_prompt = False
    else:
        accepts_prompt = "system_prompt" in signature.parameters or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()
        )
    if accepts_prompt:
        summary = call_condenser(transcript, tokens, system_prompt=system_prompt)
    else:
        # Tests and embedding callers may provide the historical two-argument hook. The
        # production function accepts the snapshot so an in-flight job cannot cross prompts.
        summary = call_condenser(transcript, tokens)
    cache_put(ident, summary, bucket, tokens, model, prompt_hash)
    bump(blocks_summarized=1)
    return ident, summary


def summary_item(ident: str, summary: str) -> dict:
    return {"role": "user", "content": [{"type": "input_text", "text": f"{FRAME}\n[block {ident}] {summary}"}]}


def schedule_summary(block: list, summarize=summarize_block) -> futures.Future:
    """Start one background condensation for a (block, model, length) identity; an identical job is reused."""
    prompt_snapshot = condenser_prompt()
    prompt_identity = condenser_prompt_hash(prompt_snapshot)
    key = (block_id(block), "deepseek", DEEPSEEK_MODEL, summary_target(), prompt_identity)
    with JOBS_LOCK:
        job = SUMMARY_JOBS.get(key)
        if job is None or job.done():
            if len(SUMMARY_JOBS) > 512:
                for old in [k for k, f in SUMMARY_JOBS.items() if f.done()]:
                    SUMMARY_JOBS.pop(old, None)
            # Capture settings before queuing; a prompt edit gets a distinct job and cache identity.
            job = SUMMARY_POOL.submit(summarize, block, key[3], key[2], prompt_snapshot, prompt_identity) if summarize is summarize_block else SUMMARY_POOL.submit(summarize, block)
            SUMMARY_JOBS[key] = job
    return job


def condense_completed(completed: list, summarize=summarize_block) -> list:
    """One summary per completed block, computed in background workers. Cached blocks answer at once; the
    request waits only WAIT_MS, then holds back whatever is still running — the worker finishes and caches it,
    so the next request reads the note instead of waiting again. A failed condenser never releases the turn."""
    jobs = [schedule_summary(block, summarize) for block in completed]
    futures.wait(jobs, timeout=WAIT_MS / 1000)
    out = []
    failed = False
    for block, job in zip(completed, jobs):
        error = job.exception() if job.done() else None
        if error is not None:
            bump(condenser_errors=1)
            if not failed:
                print(f"ctx-engine condenser unavailable ({type(error).__name__}: {error}): holding history back", file=sys.stderr, flush=True)
                failed = True
        if job.done() and error is None:
            out.append(job.result())
        else:
            bump(blocks_pending=1)
            out.append((block_id(block), PENDING_SUMMARY))
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


def rewrite_chat(body: dict, token_limit: int, raw: bytes, report: dict | None = None) -> tuple[bytes, bool]:
    prefix, blocks = split_message_blocks(body["messages"])
    if not blocks:
        return raw, False
    current, completed = blocks[-1], blocks[:-1]
    bump(blocks_seen=len(completed))
    fixed = {**body, "messages": prefix + current}
    if estimate_tokens(fixed) > token_limit:
        # The live turn alone is over budget: no summary can shrink it, so clip its biggest strings. Completed
        # blocks are condensed anyway so the request after this one finds them cached.
        if fit_live_turn(fixed, "messages", token_limit):
            bump(trimmed=1)
        if estimate_tokens(fixed) > token_limit:
            bump(overflow=1)
        for block in completed:
            schedule_summary(block)
        LAST_COMPACTION["at"] = time.time()
        encoded = compact_json(fixed).encode()
        bump(compacted=1, input_chars_sent=len(encoded))
        return encoded, True
    if not completed:
        bump(input_chars_sent=len(raw))
        return raw, False
    room = max(0, token_limit - estimate_tokens(fixed) - SUMMARY_ROOM_HEADROOM_TOKENS)
    summaries = condense_completed(completed)
    summaries = fit_summaries(summaries, room, chat_summary_item, lambda: bump(blocks_dropped=1))
    if report is not None:
        report["plan"] = compaction_plan(completed, summaries)
    rewritten = {**body, "messages": prefix + [chat_summary_item(i, t) for i, t in summaries] + current}
    encoded = compact_json(rewritten).encode()
    LAST_COMPACTION["at"] = time.time()
    bump(compacted=1, input_chars_sent=len(encoded))
    return encoded, True


def chat_summary_item(ident: str, summary: str) -> dict:
    return {"role": "user", "content": f"{FRAME}\n[block {ident}] {summary}"}


def rewrite_request(raw: bytes, token_limit: int | None = None, report: dict | None = None) -> tuple[bytes, bool]:
    token_limit = BUDGET["tokens"] if token_limit is None else token_limit
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return raw, False
    if not isinstance(body, dict):
        return raw, False
    if has_image_content(body.get("messages") or []) or has_image_content(body.get("input") or []):
        bump(images=1)
    if isinstance(body.get("messages"), list) and body.get("input") is None:
        bump(input_chars_raw=len(raw))
        if estimate_tokens(body) <= token_limit:
            bump(input_chars_sent=len(raw))
            return raw, False
        return rewrite_chat(body, token_limit, raw, report)
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
    current, completed = blocks[-1], blocks[:-1]
    bump(blocks_seen=len(completed))
    fixed = {**body, "input": current}
    if estimate_tokens(fixed) > token_limit:
        # The live turn alone is over budget: no summary can shrink it, so clip its biggest strings. Completed
        # blocks are condensed anyway so the request after this one finds them cached.
        if fit_live_turn(fixed, "input", token_limit):
            bump(trimmed=1)
        if estimate_tokens(fixed) > token_limit:
            bump(overflow=1)
        for block in completed:
            schedule_summary(block)
        LAST_COMPACTION["at"] = time.time()
        encoded = compact_json(fixed).encode()
        bump(compacted=1, input_chars_sent=len(encoded))
        return encoded, True
    if not completed:
        bump(input_chars_sent=len(raw))
        return raw, False
    room = max(0, token_limit - estimate_tokens(fixed) - SUMMARY_ROOM_HEADROOM_TOKENS)
    summaries = condense_completed(completed)
    summaries = fit_summaries(summaries, room, summary_item, lambda: bump(blocks_dropped=1))
    if report is not None:
        report["plan"] = compaction_plan(completed, summaries)
    rewritten = {**body, "input": [summary_item(i, s) for i, s in summaries] + current}
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
                prompt_hash = condenser_prompt_hash()
                for block in session["blocks"]:
                    digest = block_id(block)
                    row = conn.execute(
                        "SELECT summary FROM summaries WHERE digest = ? AND provider = 'deepseek' AND model = ? AND prompt_hash = ? "
                        "ORDER BY (tokens = ?) DESC, created_at DESC LIMIT 1",
                        (digest, DEEPSEEK_MODEL, prompt_hash, summary_target()),
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

def stats_snapshot() -> dict:
    with METRICS_LOCK:
        snapshot = dict(METRICS)
    # Row `at` is stamped by this process's clock; the dashboard renders age and the 24h window against
    # `now` from the same clock, never the browser's, so a clock-skewed host cannot shift them.
    snapshot["now"] = time.time()
    recent = recent_requests(WINDOW)
    snapshot["recent"] = recent
    snapshot["window"] = window_stats(recent)
    snapshot["global"] = global_stats()
    snapshot["series"] = reclaimed_series()
    buckets = cache_counts()
    snapshot["cache_buckets"] = buckets
    snapshot["cache_entries"] = sum(buckets.values())
    snapshot["cache_db"] = DB_PATH
    calls = snapshot["condenser_calls_deepseek"]
    snapshot["condenser_calls"] = calls
    snapshot["condenser_avg_ms"] = round(snapshot["condenser_ms_total"] / calls, 1) if calls else 0.0
    snapshot["provider"] = "deepseek"
    snapshot["model"] = DEEPSEEK_MODEL
    snapshot["token_limit"] = BUDGET["tokens"]
    snapshot["summary_tokens"] = summary_target()
    snapshot["summary_token_steps"] = list(SUMMARY_TOKEN_STEPS)
    snapshot["summary_set_at"] = SUMMARY["set_at"] or None
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
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

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
                self.send_bytes(200, compact_json(stats_snapshot()).encode())
            elif path in ("/dashboard", "/dashboard/index.html", "/dashboard/debugger"):
                self.serve_dashboard("index.html")
            elif path.startswith("/dashboard/assets/"):
                name = path.removeprefix("/dashboard/assets/")
                if not re.fullmatch(r"[A-Za-z0-9._/-]+", name) or ".." in name.split("/"):
                    self.send_bytes(404, b'{"error":"not found"}')
                    return
                self.serve_dashboard("assets/" + name)
            elif path == "/token-stats":
                try:
                    self.send_bytes(200, compact_json(token_stats()).encode())
                except (sqlite3.Error, OSError) as exc:
                    print(f"ctx-engine token stats error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    self.send_bytes(503, b'{"error":"local token journal unavailable"}')
            elif path.startswith("/request/"):
                detail = request_detail(int(path.rsplit("/", 1)[-1])) if path.rsplit("/", 1)[-1].isdigit() else None
                self.send_bytes(200 if detail else 404, compact_json(detail or {"error": "not found"}).encode())
            else:
                upstream, rest = self.resolve_upstream(path)
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
            try:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
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
            if "summary_tokens" in payload:
                try:
                    summary_tokens = int(payload["summary_tokens"])
                except (TypeError, ValueError):
                    summary_tokens = 0
                if summary_tokens not in SUMMARY_TOKEN_STEPS:
                    allowed = ", ".join(str(step) for step in SUMMARY_TOKEN_STEPS)
                    self.send_bytes(400, compact_json({"error": f"summary_tokens must be one of {allowed}"}).encode())
                    return
                result["summary_tokens"] = summary_tokens
            if not result:
                self.send_bytes(400, b'{"error":"config must set tokens and/or summary_tokens"}')
                return
            now = time.time()
            if "tokens" in result:
                BUDGET.update(tokens=result["tokens"], set_at=now)
                config_set("tokens", result["tokens"])
            if "summary_tokens" in result:
                SUMMARY.update(tokens=result["summary_tokens"], set_at=now)
                config_set("summary_tokens", result["summary_tokens"])
            self.send_bytes(200, compact_json(result).encode())
            return
        upstream, rest = self.resolve_upstream(path)
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
            body, compacted = rewrite_request(decoded, report=report)
            if not compacted:
                bump(passed=1)
            entry = {"at": time.time(), "route": route_name(path), "model": model_of(body),
                     "raw_tokens": estimate_tokens(decoded), "sent_tokens": estimate_tokens(body),
                     "result": "compacted" if compacted else "passed", "status": None, "ms": None, "detail": "",
                     "raw": decoded[:8000].decode("utf-8", "replace"), "plan": report.get("plan", "")}
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