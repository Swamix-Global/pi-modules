# ctx-engine

Loopback context-compression proxy: Bun, Elysia, native `bun:sqlite`. Runtime: `main.ts`, `condenser.ts`, `utils.ts`, `settings.ts`. Uses the existing Pi installation at `/usr/lib/node_modules/@earendil-works/pi-coding-agent` for native Codex subscription authentication. React/Vite dashboard: `frontend/`.

```sh
bun install --frozen-lockfile
bun install --cwd frontend --frozen-lockfile
bun run build
bun run check
bun test tests
bun start
```

Open **http://127.0.0.1:4141/dashboard/**.

## Dashboard

- **Overview:** compression/cache counters since restart and persisted lifetime request totals. Token counts are estimates; durations stop at compression completion or upstream response headers, not the end of streaming.
- **History:** every proxied GET/POST and `/compress` request, including unchanged and failed requests. Search by route/model, filter changed/unchanged, and load older pages through the full retained history. No automatic pruning.
- **Inspection:** complete original and forwarded request bodies, every block summary or oldest-turn drop, cache/fallback indicators, full line-by-line diff with no omitted context, wrapping and JSON download. Authorization headers are never recorded. Payloads can contain secrets, so history is sensitive local data.
- **Settings:** context threshold (64k default, with 32k/64k/96k/128k presets), protected recent turns, summary size, block prompt and model selection: Codex Luna 6 or DeepSeek Flash (low reasoning). Save explicitly. Validated updates apply to new requests and persist in SQLite; in-flight compression keeps its settings snapshot. Reset fills the form with `.env` defaults; Save applies it. Credentials, routes and DeepSeek URL remain in `.env`.

Recording starts with this dashboard deployment. Previously unrecorded payloads cannot be recovered. Old SQLite tables are preserved, but are not relabeled as new history. Interrupted requests are marked failed after restart.

## Configuration and compression

Run from the repository root. `.env` supplies startup configuration; missing settings fail startup. Dashboard overrides live in `dashboard_settings`, not in rewritten environment files. DeepSeek Flash uses the configured URL and API key or Pi's `auth.json`. Luna 6 uses Pi's newer `openai` ChatGPT-subscription credentials, with native token refresh and the direct `https://api.openai.com/v1` catalog upstream; condenser calls do not call the router recursively. Pi must already be signed in. Retired controls are removed from saved dashboard settings on startup.

Turns are grouped before compression. Over budget, all eligible older blocks launch together via `Promise.all(groups.map(...))`, and downstream waits for every block. There is no worker pool, queue or in-flight deduplication. Each uncached block gets **three total attempts**, each bounded to **10 seconds**, including authentication and response-body reads. Exhaustion logs one `CRITICAL` message with model and message hash, never payloads or credentials. Failed blocks are not cached. The configured newest turns, including tool calls/results and images, remain intact; there is no standalone tool summarizer.

Transcripts label `user:`, `assistant:`, `tool_call:`, `tool_output:`, `thinking:`. The cache key is exactly `messageHash-model-outputTokenLimit`; rendered-output hashes prevent summarizing a cached note again. No cache tags are injected. A static length instruction interpolates the configured word target and is appended to the condenser's **system prompt**, not the user transcript. Length is a sizing hint; output tokens remain DeepSeek safety headroom. The OpenAI ChatGPT-subscription connection omits hard output-token caps and follows the system word-length hint. Sampling is left to the model defaults.

There are no excerpts or character-clipping functions. Failed summaries leave the block intact. If the request still exceeds the context budget, oldest unprotected turns may be dropped; protected turns or fixed fields alone over budget return **413**, rather than corrupting their text.

## API

- `GET /health`, `GET /stats`
- `GET/POST /r/<provider>/...`: streaming proxy preserving credentials and gzip/zstd. Pi's newer `openai` provider uses base URL `http://127.0.0.1:4141/r/openai` in `~/.pi/agent/models.json`; `/r/openai/responses` forwards to `https://api.openai.com/v1/responses` after context compression. No HTTP redirect bypasses the router. For subscription bearer tokens only, the proxy omits `max_output_tokens`, `temperature`, `prompt_cache_retention` and `prompt_cache_options`, matching Pi's direct-OpenAI normalization that otherwise does not recognize loopback URLs. API-key (`sk-`) requests retain these fields.
- `GET/POST /codex/responses`: legacy Codex compatibility route, retained for existing sessions.
- `POST /compress`: returns a compressed Chat Completions or Responses body without forwarding.
- `GET/PUT /api/settings`: effective settings, `.env` defaults, validated partial updates.
- `GET /api/history?limit=50&before=<id>&changed=all|yes|no&search=<route/model>`: descending cursor pagination.
- `GET /api/history/<id>`: complete inspection record.

Dashboard APIs reject cross-origin browser requests and non-loopback hosts. This is a local service, not a public multi-tenant deployment. Accounts, provider probes, billing and the old master-session CLI remain removed.

## Development and checks

```sh
bun run --cwd frontend dev
bun run format
bun run check
bun test tests
CHROME_BIN=/path/to/chrome bun run test:ui
bun run tests/condenser.live.ts # optional: real inference with existing credentials, isolated in-memory DB
bun run tests/openai-route.live.ts # optional: native OpenAI subscription request through the deployed router
```

The browser check uses an isolated in-memory database and mock condenser. Build first; it checks metrics, pagination (including bursts larger than one page), full inspection/diffs, download, settings saves and mobile layout. `CHROME_BIN` must point to installed Chrome/Chromium. Persistence tests reopen the database in a new process, including removal of retired settings. The live check fails unless both models produce real notes and reuse their cache. For the newer OpenAI connection, use `/login openai` and choose ChatGPT subscription if authentication fails. Run `/reload` after changing `models.json`, then select `openai/gpt-6-luna`; existing `openai-codex` sessions keep the legacy route. Model catalog presence or mocked tests alone do not establish subscription access.

## Local deployment

User service `pi-context-router.service` runs Bun from this repository. Build the dashboard before restarting:

```sh
bun run build
install -m 644 pi-context-router.service ~/.config/systemd/user/pi-context-router.service
systemctl --user daemon-reload
systemctl --user enable pi-context-router.service
systemctl --user restart pi-context-router.service
curl -fsS http://127.0.0.1:4141/health
```

Enabled at user-session startup, restarting on failure. Home/system are read-only; only `~/.pi/agent` is writable for SQLite. Existing Pi local provider URLs do not change. Dashboard assets are served by the same Bun service, with no separate frontend process.
