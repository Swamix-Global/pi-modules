"""SQLite storage: settings, summary cache, and complete request history."""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
                json.dumps(trace["original"], ensure_ascii=False),
                json.dumps(trace["compressed"], ensure_ascii=False),
                json.dumps(trace["changes"], ensure_ascii=False),
                request_id,
            ),
        )

    def history(self, limit=50, before=None, changed="all", search=""):
        where = "(?='all' OR changed=?) AND (instr(lower(route),lower(?))>0 OR instr(lower(model),lower(?))>0)"
        params = (changed, int(changed == "yes"), search, search)
        columns = "id,created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error,cache_hits"
        rows = self.rows(
            f"SELECT {columns} FROM requests WHERE {where} AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
            params + (before, before, limit + 1),
        )
        total = self.rows(f"SELECT count(*) n FROM requests WHERE {where}", params)[0][
            "n"
        ]
        return {
            "items": rows[:limit],
            "total": total,
            "next": rows[limit - 1]["id"] if len(rows) > limit else None,
        }

    def detail(self, request_id):
        rows = self.rows("SELECT * FROM requests WHERE id=?", (request_id,))
        if not rows:
            return None
        row = rows[0]
        for key in ("original", "compressed", "changes"):
            row[key] = json.loads(row[key]) if row[key] is not None else None
        return row

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
