# ctx-engine

Loopback context-compression proxy: Bun, Elysia, native `bun:sqlite`. Runtime: `main.ts`, `utils.ts`, `settings.ts`. React/Vite dashboard: `frontend/`.

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
- **Inspection:** complete original and forwarded request bodies, every block/tool compression or emergency trim/drop, cache/fallback indicators, full line-by-line diff with no omitted context, wrapping and JSON download. Authorization headers are never recorded. Payloads can contain secrets, so history is sensitive local data.
- **Settings:** context/tool thresholds, protected recent turns, summary size, temperature, concurrency, fallback/clipping sizes, condenser timeout and prompts. Save explicitly. Validated updates apply to new requests and persist in SQLite; in-flight compression keeps its settings snapshot. Reset fills the form with `.env` defaults; Save applies it. Credentials, routes and condenser URL/model remain in `.env`.

Recording starts with this dashboard deployment. Previously unrecorded payloads cannot be recovered. Old SQLite tables are preserved, but are not relabeled as new history. Interrupted requests are marked failed after restart.

## Configuration and compression

Run from the repository root. `.env` supplies startup configuration; missing settings fail startup. Dashboard overrides live in `dashboard_settings`, not in rewritten environment files. Configure the condenser's OpenAI-compatible URL/model and either its API key or Pi's `auth.json`.

Turns are grouped before compression. Over budget, older turns are summarized while the configured newest turns are excluded from block summarization. Tool outputs above **100 estimated tokens** are eligible by default, including in recent turns. IDs, tool arguments, images and native wrappers are preserved. Emergency trimming may still affect recent text to meet the overall budget.

Transcripts label `user:`, `assistant:`, `tool_call:`, `tool_output:`, `thinking:`. Source/settings/output hashes are stored internally in `compressed`; no cache tags are injected. Failed condensation uses uncached excerpts. Oldest turns are dropped and oversized text clipped when necessary; immutable fields alone over budget return 413.

## API

- `GET /health`, `GET /stats`
- `GET/POST /r/<provider>/...`, `GET/POST /codex/responses`: streaming proxy preserving credentials and gzip/zstd.
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
```

The browser check uses an isolated in-memory database and mock condenser. Build first; it checks metrics, pagination (including bursts larger than one page), full inspection/diffs, download, settings saves and mobile layout. `CHROME_BIN` must point to installed Chrome/Chromium. Persistence tests reopen the database in a new process.

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
