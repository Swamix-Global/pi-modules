# ctx-engine

Loopback context-compression proxy for Pi at **http://127.0.0.1:4141**.
Fresh Python backend + Svelte 5 SPA, not a restoration of the retired Python or Bun implementations.

## Layout

```text
backend/
  db.py             SQLite storage
  utils.py          configuration, wire-format helpers, condenser
  main.py           FastAPI proxy, streaming, dashboard API
  requirements.txt
frontend/
  src/App.svelte    overview, request inspection, settings
  src/app.css       Tailwind 4
  src/main.js       Svelte entry, Font Awesome
  vite.config.js
  package.json      Svelte 5, Chart.js, Font Awesome, Tailwind
 tests/test_backend.py
pi-context-router.service
.env
```

## Setup and run

Requires Python 3.13+, Node 20.19+ (or 22.12+), npm; `uv` is used for the virtual environment.
The optional Luna condenser also requires the host's Bun and installed Pi SDK; the HTTP backend itself is Python.

```sh
uv venv .venv --python python3
uv pip install --python .venv/bin/python -r backend/requirements.txt
cd frontend && npm ci && npm run build && cd ..
.venv/bin/python -m backend.main
```

SPA: **http://127.0.0.1:4141/dashboard/**. Day/night theme, activity chart, auto-refreshing request history, full before/after JSON inspection with copy/download, and persistent settings.
Vite development: `cd frontend && npm run dev`; the dev server proxies API calls to :4141.

### Checks

```sh
.venv/bin/python -m unittest discover -s tests -v
cd frontend && npm run check && npm run build
```

Tests are offline with mocked inference and upstreams: no provider credentials or charges. They cover storage persistence, interruption recovery, strict cache keys, protected turns and tool linkage, safe older-activity reductions despite query/protected/image/fixed overflow, non-expanding notes, Responses format, parallel fanout, timeouts, fail-fast failure messages, body encodings, streaming, subscription normalization, history pagination, and database failure status.
Rendered UI regression check (a Chromium headless shell is available on this host):

```sh
.venv/bin/python tests/test_ui.py
```

### Deploy

```sh
install -m 644 pi-context-router.service ~/.config/systemd/user/pi-context-router.service
systemctl --user daemon-reload
systemctl --user restart pi-context-router.service
curl -fsS http://127.0.0.1:4141/health
journalctl --user -u pi-context-router.service -n 20 --no-pager
```

Build before restarting after frontend changes. The service binds loopback only, uses a restrictive umask, and can write only under `~/.pi/agent`.

## Configuration and proxy

`.env` supplies defaults; process environment takes precedence. Persisted dashboard settings override the defaults on restart. Default soft threshold: **32,000 estimated tokens**; keep **two recent turns**, output safety cap **8192 tokens**. Estimates are Unicode characters / 3.5, excluding image data; they are not provider billing counts.

- `/r/<provider>/<path>` joins the path and query onto the provider URL in `PI_CONTEXT_ROUTER_ROUTES`. Incoming authorization goes only to that selected upstream, never into SQLite or logs. Unknown providers return 404. Antigravity routes through `/r/antigravity` as a secure pass-through: the proxy securely forwards upstream over HTTPS, preserving non-OpenAI wire formats intact.
- `/codex/responses` remains supported for older Pi sessions. Pi's `models.json` does not need to change.
- Non-`sk-` subscription bearers on `/r/openai/responses` and `/codex/responses` strip `max_output_tokens`, `temperature`, `prompt_cache_retention`, and `prompt_cache_options`. API-key requests keep them.
- Upstream bodies are streamed raw, including SSE and compressed responses. gzip/zstd requests are decoded only for inspection/compression and re-encoded only when changed. Empty-body endpoints bypass JSON parsing. Context overflow never returns 413; excessive transport byte sizes are a separate 400 safety limit.
- Dashboard APIs require same-origin browser access. This is a sensitive, loopback-only local tool, not a publicly authenticated service.

## Condensation

Every condensed older turn has exactly this shape in both Chat Completions `messages` and Responses `input`:

```text
user:      original query, verbatim
assistant: one summary of all subsequent assistant replies, reasoning and tool calls/results
```

The opening query is not sent to the condenser and is never rewritten or replaced by a summary. Its whitespace, Unicode, structured content and message metadata remain intact. Query-only turns are not summarized and never get an invented assistant response. Complete older assistant activity blocks condense in parallel; all tasks settle before forwarding. The newest configured turns and system/developer prefix remain intact. Image-containing turns stay intact.

Consecutive real user messages concatenate into one query with a newline between them, even below the threshold; each original text and content part is preserved. User-role tool-result envelopes are not queries and remain inside the activity group. Protected-turn counting happens after concatenation. Every verbatim query is indivisible fixed context. If queries plus protected/fixed fields already overflow, keep them verbatim and still condense eligible older assistant activity. A large newest turn, image block or fixed prefix must not disable useful reductions elsewhere; the threshold is a soft target, not a reason to abandon condensation or truncate protected content. The request history records concatenations as `merge_users` changes.

DeepSeek Flash uses API credentials from `PI_CONTEXT_ROUTER_API_KEY` or Pi's existing `auth.json`, with explicit thinking disabled to preserve low latency and output token budget. Condensers are the local `minicpm5-2b-fp8` server and Z.ai `glm-5.3-flash`, both over the OpenAI chat-completions wire format with thinking disabled; no child process, CLI agent loop or installed-core patch.

Each uncached block has **three total attempts, 30 seconds each**. Empty/failed/timed-out output is never cached. Exhaustion never fails the turn: the original block is forwarded verbatim (payload-free CRITICAL in the journal) and the condenser cools down for `PI_CONTEXT_ROUTER_CONDENSER_COOLDOWN` seconds (default 120), so later blocks skip it instead of stalling. No clipped excerpts, silent dropping, fabricated summaries, or idle warming. If verbatim queries plus protected/fixed context alone exceed the soft threshold, warn and continue safe older-activity condensation. If the resulting request still exceeds it, forward the complete result and warn; do not force it under budget. A generated note replaces its original block only when the complete replacement (verbatim query + note) reduces estimated tokens. Otherwise retain the entire original block and keep the canonical note cached, so verbose summaries cannot expand upstream context or cause repeated inference. This is a per-block size check, never a clipping rule.

Cache keys are exactly `<SHA256 canonical assistant activity block>-<model>-<output cap>`: the opening query is excluded from both the cache source and condenser transcript. The new activity-only keys do not reuse the former user-plus-activity notes; old cache rows remain preserved. The sizing hint is `100 + ceil(estimated activity transcript tokens / 10)`; it is not a truncation rule. The first committed note is canonical when concurrent requests race. Existing rendered assistant notes are recognized to avoid recondensing them. Settings are snapshotted once per incoming request. Persisted dashboard settings are not reset by code deployment. Native Pi compaction is separate and may still bill for its input; proxy estimates and reductions are not provider billing counts.

## SQLite and request logs

Fresh database: `~/.pi/agent/ctx-engine-v2.db` (override `PI_CONTEXT_ROUTER_DB`). Three application tables, plus SQLite's sequence table:

| Table | Purpose |
| --- | --- |
| `settings` | key → JSON value |
| `cache` | strict key, output, output hash, creation timestamp |
| `requests` | request metadata and full original/forwarded JSON plus changes |

WAL, 5-second busy timeout, 0600 main database. No migrations or legacy table reuse. No automatic deletion/pruning of payloads. Request rows begin before inference, preserve originals before slow operations, then finalize with status/duration/error. Streams finalize at completion; a restart marks unfinished rows 503 rather than leaving them permanently in-flight. Every proxied request also emits a payload-free journal line. SQLite failures are 503, never bad credentials. `/health` runs a fresh SQLite quick check.

The pre-rewrite source and a consistent SQLite backup are stored under `~/.pi/agent/backups/ctx-engine-20261002-103354/`. The original `~/.pi/agent/ctx-engine.db` is preserved but not opened by this implementation. It passed full integrity checks; the obsolete `requests` table had stopped receiving writes when the previous backend changed. New history starts at this deployment; it does not fabricate missing historical payloads.

Full stored payloads and downloaded inspection JSON may contain sensitive prompt data. Credentials in HTTP headers are never persisted. Protect or remove backups manually when no longer needed.
