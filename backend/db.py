"""SQLite storage: settings, summary cache, and delta-optimized request history."""

import copy
import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
import zstandard

LOG = logging.getLogger("ctx-engine")

_CCTX = zstandard.ZstdCompressor(level=3)
_DCTX = zstandard.ZstdDecompressor()
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
ONE_DAY_MS = 86_400_000  # 24 hours in milliseconds


def zstd_compress(data):
    """Compresses data into binary Zstandard blob. Never leaves raw text."""
    if data is None:
        return None
    if isinstance(data, (bytes, bytearray)):
        if data.startswith(ZSTD_MAGIC):
            return bytes(data)
        return _CCTX.compress(bytes(data))
    if isinstance(data, str):
        raw = data.encode("utf-8")
    else:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return _CCTX.compress(raw)


def zstd_decompress(blob):
    """Decompresses binary Zstandard blob or parses JSON/text."""
    if blob is None:
        return None
    if isinstance(blob, (bytes, bytearray)):
        if blob.startswith(ZSTD_MAGIC):
            raw = _DCTX.decompress(bytes(blob))
        else:
            raw = bytes(blob)
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return raw.decode("utf-8", errors="replace")
    if isinstance(blob, str):
        try:
            return json.loads(blob)
        except (ValueError, UnicodeDecodeError):
            return blob
    return blob


def calculate_delta(original, compressed, changes=None):
    """Calculates concise delta representation between original and compressed payloads."""
    if changes is None:
        changes = []
    if not changes and original == compressed:
        return {
            "changed": False,
            "delta_tokens": 0,
            "summary": "identical",
            "changes": [],
        }

    delta = {
        "changed": bool(changes) or (original != compressed),
        "changes_count": len(changes),
        "changes": changes,
    }

    if isinstance(original, dict) and isinstance(compressed, dict):
        removed_keys = [k for k in original if k not in compressed]
        if removed_keys:
            delta["removed_keys"] = removed_keys
        field = (
            "messages"
            if "messages" in original
            else "input"
            if "input" in original
            else None
        )
        if (
            field
            and isinstance(original.get(field), list)
            and isinstance(compressed.get(field), list)
        ):
            delta["turn_counts"] = {
                "before": len(original[field]),
                "after": len(compressed[field]),
            }
            condensed = [
                c
                for c in changes
                if isinstance(c, dict) and c.get("kind") == "block"
            ]
            if condensed:
                delta["condensed_blocks"] = len(condensed)

    return delta


class InMemoryZstdStore:
    """In-memory storage keeping recent (<1d) items uncompressed and older (>=1d) compressed."""

    def __init__(self, max_items=2000):
        self._store = {}  # key -> (created_at_ms, is_compressed, data)
        self._max_items = max_items
        self.ONE_DAY_MS = ONE_DAY_MS

    def put(self, key, value, created_at_ms=None):
        if created_at_ms is None:
            created_at_ms = int(time.time() * 1000)

        now_ms = int(time.time() * 1000)
        is_old = (now_ms - created_at_ms) >= self.ONE_DAY_MS

        if is_old:
            self._store[key] = (created_at_ms, True, zstd_compress(value))
        else:
            self._store[key] = (created_at_ms, False, value)

        if len(self._store) > self._max_items:
            oldest_key = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest_key]

    def get(self, key):
        if key not in self._store:
            return None
        created_at_ms, is_compressed, data = self._store[key]
        now_ms = int(time.time() * 1000)

        # Aging in memory: compress if it crossed 1 day
        if not is_compressed and (now_ms - created_at_ms) >= self.ONE_DAY_MS:
            data = zstd_compress(data)
            is_compressed = True
            self._store[key] = (created_at_ms, True, data)

        if is_compressed:
            return zstd_decompress(data)
        return copy.deepcopy(data)

    def is_compressed(self, key):
        return self._store[key][1] if key in self._store else False

    def compress_old_entries(self):
        now_ms = int(time.time() * 1000)
        compressed_count = 0
        for key, (created_at_ms, is_compressed, data) in list(self._store.items()):
            if not is_compressed and (now_ms - created_at_ms) >= self.ONE_DAY_MS:
                self._store[key] = (created_at_ms, True, zstd_compress(data))
                compressed_count += 1
        return compressed_count

    def clear(self):
        self._store.clear()


class Database:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._mem_store = InMemoryZstdStore()
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY, output TEXT NOT NULL,
                    output_hash TEXT NOT NULL, created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS cache_output ON cache(output_hash);
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at INTEGER NOT NULL, route TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '', status INTEGER NOT NULL DEFAULT 0,
                    changed INTEGER NOT NULL DEFAULT 0,
                    before_tokens INTEGER NOT NULL DEFAULT 0,
                    after_tokens INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    cache_hits INTEGER NOT NULL DEFAULT 0,
                    error TEXT, original TEXT, compressed TEXT,
                    changes TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS requests_time ON requests(created_at);
                CREATE INDEX IF NOT EXISTS requests_metrics ON requests(
                    created_at,status,changed,before_tokens,after_tokens,duration_ms,cache_hits
                );
                CREATE INDEX IF NOT EXISTS requests_status ON requests(status);
                CREATE INDEX IF NOT EXISTS requests_changed_id ON requests(changed, id DESC);
                CREATE INDEX IF NOT EXISTS requests_history_cov ON requests(
                    id DESC, model, route, changed, created_at, status, before_tokens, after_tokens, duration_ms, cache_hits, error
                );
            """)
            # A process restart cannot leave old attempts pretending to be live.
            db.execute(
                "UPDATE requests SET status=503,error='Router restarted before completion' WHERE status=0"
            )
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def rows(self, sql, args=()):
        with self.connect() as db:
            return [dict(row) for row in db.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        with self.connect() as db:
            return db.execute(sql, args).lastrowid

    def health(self):
        with self.connect() as db:
            db.execute("PRAGMA schema_version").fetchone()
            return "ok"

    def quick_check(self):
        with self.connect() as db:
            return db.execute("PRAGMA quick_check(1)").fetchone()[0]

    def settings(self, defaults):
        result = dict(defaults)
        for row in self.rows("SELECT key,value FROM settings"):
            if row["key"] in defaults:
                result[row["key"]] = json.loads(row["value"])
        return result

    def save_settings(self, settings):
        with self.connect() as db:
            db.executemany(
                "INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [(key, json.dumps(value)) for key, value in settings.items()],
            )

    def cache_get(self, key):
        rows = self.rows("SELECT output FROM cache WHERE key=?", (key,))
        return rows[0]["output"] if rows else None

    def cache_put(self, key, output, output_hash):
        self.execute(
            "INSERT OR IGNORE INTO cache VALUES(?,?,?,?)",
            (key, output, output_hash, int(time.time() * 1000)),
        )
        # Return the canonical first note even if parallel requests raced.
        return self.cache_get(key)

    def is_summary(self, digest):
        return bool(
            self.rows("SELECT 1 FROM cache WHERE output_hash=? LIMIT 1", (digest,))
        )

    def start_request(self, route):
        return self.execute(
            "INSERT INTO requests(created_at,route) VALUES(?,?)",
            (int(time.time() * 1000), route),
        )

    def finish_request(self, request_id, trace):
        orig_blob = zstd_compress(trace["original"])
        # Smartest strategy: do not store duplicate compressed payload if unchanged
        comp_blob = zstd_compress(trace["compressed"]) if trace["changed"] else None
        changes_json = (
            json.dumps(trace["changes"], ensure_ascii=False)
            if trace["changes"] is not None
            else "[]"
        )
        self.execute(
            """UPDATE requests SET model=?,status=?,changed=?,before_tokens=?,
            after_tokens=?,duration_ms=?,cache_hits=?,error=?,original=?,compressed=?,changes=? WHERE id=?""",
            (
                trace["model"],
                trace["status"],
                int(trace["changed"]),
                trace["before_tokens"],
                trace["after_tokens"],
                trace["duration_ms"],
                trace["cache_hits"],
                trace["error"],
                orig_blob,
                comp_blob,
                changes_json,
                request_id,
            ),
        )

        now_ms = int(time.time() * 1000)
        detail_obj = {
            "id": request_id,
            "created_at": now_ms,
            "route": trace.get("route", ""),
            "model": trace["model"],
            "status": trace["status"],
            "changed": int(trace["changed"]),
            "before_tokens": trace["before_tokens"],
            "after_tokens": trace["after_tokens"],
            "duration_ms": trace["duration_ms"],
            "cache_hits": trace["cache_hits"],
            "error": trace["error"],
            "original": trace["original"],
            "compressed": trace["compressed"],
            "changes": trace["changes"],
            "delta": calculate_delta(
                trace["original"], trace["compressed"], trace["changes"]
            ),
        }
        self._mem_store.put(f"req:{request_id}", detail_obj, created_at_ms=now_ms)

    def history(self, limit=50, before=None, changed="all", search=""):
        clauses = []
        params = []
        count_params = []

        if changed in ("yes", "no"):
            clauses.append("changed=?")
            val = 1 if changed == "yes" else 0
            params.append(val)
            count_params.append(val)

        if search and search.strip():
            clauses.append(
                "(instr(lower(route),lower(?))>0 OR instr(lower(model),lower(?))>0)"
            )
            s = search.strip()
            params.extend([s, s])
            count_params.extend([s, s])

        count_where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        if before is not None:
            clauses.append("id < ?")
            params.append(before)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        columns = "id,created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error,cache_hits"
        rows = self.rows(
            f"SELECT {columns} FROM requests {where} ORDER BY id DESC LIMIT ?",
            tuple(params) + (limit + 1,),
        )
        total = self.rows(
            f"SELECT count(*) n FROM requests {count_where}",
            tuple(count_params),
        )[0]["n"]
        return {
            "items": rows[:limit],
            "total": total,
            "next": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def detail(self, request_id):
        cached = self._mem_store.get(f"req:{request_id}")
        if cached is not None:
            return cached

        rows = self.rows("SELECT * FROM requests WHERE id=?", (request_id,))
        if not rows:
            return None
        row = rows[0]
        orig = zstd_decompress(row["original"])
        row["original"] = orig

        if row["compressed"] is not None:
            row["compressed"] = zstd_decompress(row["compressed"])
        else:
            row["compressed"] = copy.deepcopy(orig)

        if row["changes"] is not None:
            row["changes"] = (
                json.loads(row["changes"])
                if isinstance(row["changes"], str)
                else row["changes"]
            )
        else:
            row["changes"] = []

        row["delta"] = calculate_delta(
            row["original"], row["compressed"], row["changes"]
        )
        self._mem_store.put(
            f"req:{request_id}", row, created_at_ms=row["created_at"]
        )
        return row

    def compact_old_data(self, max_age_days=1, batch_size=1000):
        """Compacts requests older than max_age_days into zstd blobs and nulls redundant payloads."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - int(max_age_days * ONE_DAY_MS)
        mem_compacted = self._mem_store.compress_old_entries()
        compacted_db_rows = 0

        with self.connect() as db:
            cursor = db.execute(
                """SELECT id, changed, typeof(original), original,
                          CASE WHEN changed = 1 THEN compressed ELSE NULL END as comp_val,
                          CASE WHEN changed = 1 THEN typeof(compressed) ELSE 'null' END as comp_type
                   FROM requests
                   WHERE created_at < ? AND (typeof(original) = 'text' OR (changed = 0 AND compressed IS NOT NULL))
                   LIMIT ?""",
                (cutoff_ms, batch_size),
            )
            rows = cursor.fetchall()
            for r in rows:
                rid, changed, orig_type, orig_val, comp_val, comp_type = r
                new_orig = (
                    zstd_compress(orig_val) if orig_type == "text" else orig_val
                )
                new_comp = (
                    None
                    if changed == 0
                    else (
                        zstd_compress(comp_val)
                        if comp_type == "text"
                        else comp_val
                    )
                )
                db.execute(
                    "UPDATE requests SET original=?, compressed=? WHERE id=?",
                    (new_orig, new_comp, rid),
                )
                compacted_db_rows += 1

        return {"db_rows": compacted_db_rows, "memory_entries": mem_compacted}

    def stats(self, range="24h"):
        now = int(time.time() * 1000)
        horizons = {"24h": 86400000, "7d": 604800000, "30d": 2592000000}
        if range not in horizons:
            raise ValueError("Invalid stats range")

        def aggregate(since=None):
            where, args = ("WHERE created_at>=?", (since,)) if since is not None else ("", ())
            return self.rows(
                f"""SELECT count(*) count,coalesce(sum(status>=400),0) errors,
                coalesce(sum(changed),0) compacted,coalesce(sum(before_tokens),0) "in",
                coalesce(sum(after_tokens),0) "out",coalesce(sum(duration_ms),0) duration_ms,
                coalesce(max(created_at)-min(created_at),0) span_ms
                FROM requests INDEXED BY requests_metrics {where}""",
                args,
            )[0]

        history = self.rows("""SELECT count(*) requests,coalesce(sum(changed),0) compacted,
            coalesce(sum(status>=400),0) errors,coalesce(sum(before_tokens),0) before_tokens,
            coalesce(sum(after_tokens),0) after_tokens,coalesce(sum(duration_ms),0) duration_ms
            FROM requests INDEXED BY requests_metrics""")[0]
        windows = {
            key: aggregate(now - span) for key, span in
            (("minute", 60000), ("hour", 3600000), ("day", 86400000))
        }
        windows["all"] = aggregate()
        bucket, horizon = {
            "24h": (300000, 86400000),
            "7d": (3600000, 604800000),
            "30d": (86400000, 2592000000),
        }[range]
        timeline = self.rows(
            f"""SELECT (created_at/{bucket})*{bucket} time,count(*) requests,
            sum(changed) compacted,sum(status>=400) errors,sum(before_tokens) before_tokens,
            sum(after_tokens) after_tokens FROM requests INDEXED BY requests_metrics
            WHERE created_at>=? GROUP BY time ORDER BY time""",
            (now - horizon,),
        )
        return {
            "now": now,
            "history": history,
            "cache": {"n": self.rows("SELECT count(*) n FROM cache")[0]["n"]},
            "timeline": timeline,
            "windows": windows,
        }
