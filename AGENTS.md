# Project Agent Instructions: ctx-engine

Fresh loopback context-compression proxy: Python/FastAPI/httpx/sqlite3 in `backend/`, Svelte 5/Tailwind 4/Font Awesome/Chart.js SPA in `frontend/`.

## Layout and interfaces

- `backend/db.py`: SQLite only, three application tables (`settings`, `cache`, `requests`), WAL and 5-second busy timeout. Fresh DB `~/.pi/agent/ctx-engine-v2.db`; never silently reuse, overwrite, or drop the archived old database.
- `backend/utils.py`: configuration, transcript/turn helpers, `ctx_man`, `compress_body`. DeepSeek Flash directly; Luna 6 through a one-shot supported Pi SDK child with native OAuth, never routed through the proxy.
- `backend/main.py`: explicit FastAPI proxy/dashboard routes, raw upstream streaming, persistent request attempts, same-origin management APIs.
- Frontend source is `frontend/src/`; no React, Elysia, Drizzle, or old Bun backend remains.

## Invariants

- Soft token threshold default 32k; preserve protected turns and fixed fields. Indivisible overflow does not disable condensation of eligible older activity; warn when the protected floor or final result exceeds the target. No context-driven 413, clipping, silent block dropping, or excerpt fallback.
- Completed turn blocks launch concurrently and all settle. Three total attempts × 30 seconds per uncached block. Exhaustion never fails the turn: emit a payload-free CRITICAL, forward that block verbatim, and cool the condenser down for `PI_CONTEXT_ROUTER_CONDENSER_COOLDOWN` seconds (default 120) so later blocks skip it rather than stall.
- Each condensed turn is the original user query verbatim as a `user` message, followed by one `assistant` summary of only the subsequent replies, reasoning and tool calls/results. Never send the opening query to the condenser or replace it with a summary. Query-only turns do not invent an assistant reply.
- Concatenate consecutive real user messages with one newline, preserving text/whitespace and structured content parts; do this even below threshold. User-role tool-result envelopes are activity, not queries. Protected turns count after concatenation.
- Cache key strictly `<assistant activity block SHA256>-<model>-<output cap>`; concurrent misses return the first committed note. Old whole-turn cache keys are not reused for assistant-only notes. Recognize already-rendered assistant notes. Sizing hints never truncate output.
- Tool calls/results stay together; image-containing blocks remain intact; encrypted reasoning is not rendered to the condenser. All verbatim queries are indivisible fixed context; preserve them even above the soft budget, while still condensing other eligible older activity. Accept a note only if its replacement block reduces estimated tokens; otherwise retain the entire original block, without clipping the note, and reuse its canonical cache entry on later attempts.
- Subscription-authenticated `/r/openai/responses` and `/codex/responses` strip `max_output_tokens`, `temperature`, `prompt_cache_retention`, and `prompt_cache_options`; `sk-` payloads retain them.
- Antigravity requests can route through `/r/antigravity` as a secure pass-through: the proxy server securely forwards requests upstream to Google Cloud over HTTPS without mutating proprietary non-OpenAI wire formats.
- Persist attempts and full received/forwarded JSON, not credentials; no automatic pruning. Log each proxy decision in the journal. Database failures return 503. Health probes SQLite cheaply; /health?deep=1 runs the full integrity check. Restarting marks unfinished rows 503.
- SPA timestamps use system locale; chart anchors to server `now`; tabs/request selection persist in URL. Themes must remain high-contrast.

## Canonical checks/deploy

```sh
.venv/bin/python -m unittest discover -s tests -v
cd frontend && npm run check && npm run build
.venv/bin/python tests/test_ui.py
systemctl --user restart pi-context-router.service
curl -fsS http://127.0.0.1:4141/health
```

Run backend tests for any Python changes. Build and restart after frontend changes. See `README.md` for installation, backup location, architecture and deployment; do not duplicate process guides. Follow global agent rules and `~/.pi/agent/AGENTS-RSI.md` for stack changes. Keep existing unrelated user files intact; commit changes every time files are modified, and show the diff between commits.

## Project Summarization (standard recipe)
- Goal: per-file ~200-word structured description of every source file.
- Output: one YAML doc mapping `filename` → `purpose` (2 sentences), `components` (5–6), `key_functions` (8–10, real symbol names), `data_flow` (3–4 steps), `interactions` (2–3).
- Endpoint `http://127.0.0.1:30000/v1/chat/completions`, model per serving (e.g. `minicpm5-2b-fp8`), `enable_thinking: false`, `max_tokens 2048`.
- Sampling: `temperature 0` deterministic; general profile `temperature 0.3, repetition_penalty 1.05`; never >= 1.1; top_p is noise.
- Parallelism: ThreadPoolExecutor(48–128); server tuned to 128 streams (5576 tok/s aggregate).
- Formatting enforced by code: lenient line-parse, rebuild the 5-key dict, `yaml.safe_dump`, round-trip validate. Write to `lang-test.txt`; report per-call e2e + wall total.
