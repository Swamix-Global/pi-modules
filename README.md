# ctx-engine

Loopback compression proxy using **Bun + Elysia + native `bun:sqlite`**.
Runtime: `main.ts`, `utils.ts`, `settings.ts` in the repository root. No Python.

```sh
bun install --frozen-lockfile
bun run format
bun run check
bun test tests
bun start
curl -fsS http://127.0.0.1:4141/health
```

All configuration is in `.env`; missing settings fail startup. Run from the repository root.
Configure the condenser's OpenAI-compatible URL/model and either its API key or Pi's `auth.json`.

- `/r/<provider>/...` and `/codex/responses` proxy GET/POST, preserving streaming, credentials and gzip/zstd.
- `POST /compress` accepts a Chat Completions or Responses body and returns the compressed body. This can call the configured condenser.
- `GET /stats` exposes compression counters and cache size.
- Split on user turns **before** compression. Over budget, compress older turns while keeping the configured newest turns together; compress tool outputs above **100 estimated tokens** without changing call IDs or tool arguments.
- Condenser transcripts label `user:`, `assistant:`, `tool_call:`, `tool_output:`, `thinking:` with `---` between sections and `---------------------------` between turns.
- SQLite stores source/settings/output hashes internally. No cache tags or markers are injected. Identical rendered outputs are recognized without modifying their text.
- Failed condensation uses uncached clipped excerpts; oversized remaining text is clipped, oldest turns dropped first. If immutable fields alone exceed the budget, return 413 rather than forwarding over budget.

The old dashboard, accounts, provider probes, billing and master-session CLI are intentionally removed. This is a loopback-only compression backend, not a public multi-tenant service. Existing SQLite tables are preserved; the rewrite uses a new `compressed` table.

Local deployment uses the user service `pi-context-router.service`, running Bun from this repository and reading `.env` (no legacy service environment overrides). Install or update it with:

```sh
install -m 644 pi-context-router.service ~/.config/systemd/user/pi-context-router.service
systemctl --user daemon-reload
systemctl --user enable pi-context-router.service
systemctl --user restart pi-context-router.service
curl -fsS http://127.0.0.1:4141/health
```

The unit is enabled for user-session startup and restarts on failure. It retains read-only home/system protections, allowing writes only under `~/.pi/agent` for the database. Pi's existing local provider URLs require no changes. The retired dashboard and other removed endpoints are not deployed.
