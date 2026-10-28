# ctx-engine — stateless N-1 context router

Transparent HTTP router in front of every inference host. One job: keep prompts
small and byte-stable by stripping completed tool history — unconditionally, with
no token budgets and no per-request state.

## Shape (fixed)
- `backend/app.py` — FastAPI proxy, `minify()` orchestration, `/health` `/stats`, response chaining.
- `backend/config.py` — every environment tunable, read once at import; restart to change.
- `backend/utils.py` — wire/auth/gzip helpers, media stripping, tool-pair surgery.
- `main.py` — thin uvicorn entry (`python -m main`); `backend` is a namespace package.

No new modules, no `__init__.py`, no helper libraries. Functionality lives in
exactly these three backend files.

## Core rule: unconditional N-1 tool stripping
Every parsed request body is treated the same way, regardless of size:
- keep the **newest complete pre-turn tool pair** (call + its results);
- drop **every older complete pair**;
- strip media blocks (image/file/audio) from history;
- never cut prose (user/assistant text) and never touch the current turn;
- repair dangling calls/results so the wire stays provider-valid.

Nothing to strip → bytes forwarded untouched (`unchanged`). The same transcript
always maps to the same prompt bytes; that determinism is the product — provider
prompt caches stay warm because the router's output never depends on request
history or size estimates.

## Non-negotiables
- Stateless: no sessions, no frontier files, no condenser, no caches keyed by anything.
- No token budgets, no char/token estimation driving behavior.
- Only `config.py` reads the environment.
- Providers never see router internals: hop-by-hop headers stripped, bearer auth
  injected from `~/.pi/agent/auth.json` for managed providers, private `x-pi-*`
  headers removed.
- Upstream errors pass through untouched; the router never rewrites errors.

## Verification: manual telemetry only
No unit tests, no mocks, no pytest — deleted by policy; do not add them. Prove
every change with a real Pi instance driven over RPC (`scripts/manual_rpc.py`):
real turns, real provider usage. Read `/stats` (`requests`, `unchanged`,
`stripped`, `pairs_dropped`, `media_removed`, `usage_tokens`, `last_request`)
plus provider `cacheRead` growth from actual usage rows. Nothing else counts as
verification.

Restart after backend changes: `systemctl --user restart pi-context-router`.
