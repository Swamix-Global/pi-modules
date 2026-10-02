# Project Agent Instructions: ctx-engine

Fresh loopback context-compression proxy: Python/FastAPI/httpx/sqlite3 in `backend/`, Svelte 5/Tailwind 4/Font Awesome/Chart.js SPA in `frontend/`.

## Layout and interfaces

- `backend/db.py`: SQLite only, three application tables (`settings`, `cache`, `requests`), WAL and 5-second busy timeout. Fresh DB `~/.pi/agent/ctx-engine-v2.db`; never silently reuse, overwrite, or drop the archived old database.
- `backend/utils.py`: configuration, transcript/turn helpers, `ctx_man`, `compress_body`. DeepSeek Flash directly; Luna 6 through a one-shot supported Pi SDK child with native OAuth, never routed through the proxy.
- `backend/main.py`: explicit FastAPI proxy/dashboard routes, raw upstream streaming, persistent request attempts, same-origin management APIs.
- Frontend source is `frontend/src/`; no React, Elysia, Drizzle, or old Bun backend remains.

## Invariants

- Soft token threshold default 32k; preserve protected turns and fixed fields. Forward intact with warnings when indivisible context exceeds it. No context-driven 413, clipping, silent block dropping, or excerpt fallback.
- Completed turn blocks launch concurrently and all settle. Three total attempts × 10 seconds per uncached block. Exhaustion emits payload-free CRITICAL and returns HTTP 502 with exact verbatim user message: `Summarization failed for turn: "..."`.
- Cache key strictly `<canonical block SHA256>-<model>-<output cap>`; concurrent misses return the first committed note. Sizing hints never truncate output.
- Tool calls/results stay together; image-containing blocks remain intact; encrypted reasoning is not rendered to the condenser.
- Subscription-authenticated `/r/openai/responses` and `/codex/responses` strip `max_output_tokens`, `temperature`, `prompt_cache_retention`, and `prompt_cache_options`; `sk-` payloads retain them.
- Only OpenAI-compatible wire formats route here; never route Antigravity's proprietary Protobuf API.
- Persist attempts and full received/forwarded JSON, not credentials; no automatic pruning. Log each proxy decision in the journal. Database failures return 503. Health measures SQLite fresh; restarts finalize unfinished rows.
- SPA timestamps use system locale; chart anchors to server `now`; tabs/request selection persist in URL. Themes must remain high-contrast.

## Canonical checks/deploy

```sh
.venv/bin/python -m unittest discover -s tests -v
cd frontend && npm run check && npm run build
.venv/bin/python tests/test_ui.py
systemctl --user restart pi-context-router.service
curl -fsS http://127.0.0.1:4141/health
```

Run backend tests for any Python changes. Build and restart after frontend changes. See `README.md` for installation, backup location, architecture and deployment; do not duplicate process guides. Follow global agent rules and `~/.pi/agent/AGENTS-RSI.md` for stack changes. Keep existing unrelated user files intact; checkpoint meaningful progress in Git.
