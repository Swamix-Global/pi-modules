# ctx-engine

Transparent context router on `127.0.0.1:4141` (`pi-context-router.service`,
systemd --user). Stateless N-1 tool stripping for every chat-completion request:
keep the newest complete pre-turn tool pair, drop all older pairs, strip media
blocks, never cut prose or the current turn, repair dangling tool call/result
pairing. No token budgets, no thresholds, no per-request state — the same
transcript maps to the same prompt bytes on every request, keeping provider
prompt caches warm. Bodies with nothing to strip are forwarded byte-identical.

## Layout
- `backend/app.py` — FastAPI proxy, minify orchestration, health/stats telemetry.
- `backend/config.py` — all environment tunables (host/port, upstream auth,
  gzip, codex/sglang routes), read once at import.
- `backend/utils.py` — auth/header/gzip wire helpers and tool-pair surgery.
- `main.py` — uvicorn entrypoint; `backend` is a namespace package.
- `scripts/manual_rpc.py` — the only accepted verification: drives a real Pi
  instance over RPC and reports router telemetry. No unit tests exist.

## Run / verify
```sh
systemctl --user restart pi-context-router   # after backend changes
curl -s localhost:4141/stats | python3 -m json.tool
```
See `AGENTS.md` for invariants and the manual-telemetry policy, `SPEC.md` for
the wire-level design.
