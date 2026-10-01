# Project Agent Instructions: ctx-engine

Loopback context-compression proxy for Pi Coding Agent (`http://127.0.0.1:4141`), built with Bun, Elysia, and Drizzle ORM (`bun-sqlite`).

## Architecture & Interfaces

- **`ctx_man(msgs_block, model, outlen)`** (`condenser.ts`): Unified entry point for block condensation.
  - Dual models: Codex Luna 6 (`gpt-6-luna`, native Pi OAuth) and DeepSeek Flash (`deepseek-flash`, low reasoning).
  - Retry policy: 3 total attempts &times; 10s timeout per uncached block. Logs `CRITICAL` on exhaustion.
  - Fail fast: If condensation fails, throw downstream HTTP 502 with the exact verbatim user message (`Summarization failed for turn: "${userMsg}"`). Never silently drop or excerpt blocks.
  - Cache key: Strictly `${hash(msgs_block)}-${model}-${outlen}`.
  - Fanout: Unbounded `Promise.all(groups.map(...))` launched in parallel.
- **`db_man(query)`** (`utils.ts`): Unified database access layer wrapping Drizzle ORM (`drizzle-orm/bun-sqlite`) and prepared statements against SQLite (`~/.pi/agent/ctx-engine.db`).

## Invariants & Rules

- **Soft Token Threshold:** Context budget (default 64,000 / `64k`) is soft. If indivisible protected turns or fixed fields exceed the budget, forward unchanged with a warning log. Never reject requests with HTTP 413.
- **Zero Silent Fallbacks:** No excerpt clipping, character truncation, or silent block dropping. Either preserve blocks intact or fail fast to protect prompt cache prefixes.
- **OpenAI Subscription Normalization:** In `/r/openai` and `/codex/responses`, non-`sk-` OAuth bearer tokens strip `max_output_tokens`, `temperature`, `prompt_cache_retention`, and `prompt_cache_options` to prevent 400 errors from OpenAI's endpoint.
- **Provider Boundaries:** Only providers with OpenAI-compatible Chat Completions or Responses wire format route through `ctx-engine`. Google Cloud Code (`pi-antigravity`) speaks proprietary Protobuf/SSE directly over HTTPS and is deliberately not routed through `:4141`.
- **Dashboard UI:** React/Vite in `frontend/`, served at `http://127.0.0.1:4141/dashboard/`. Uses TanStack Table, Lucide icons, localized system timestamps (`toLocaleTimeString`), and URL state preservation (`?tab=...&request=...`).

## Canonical Commands

- **Build frontend:** `bun run build`
- **Typecheck:** `bun run check`
- **Unit test suite:** `bun test tests/compression.test.ts tests/condenser.test.ts tests/persistence.test.ts`
- **Live tests (optional):** `bun run tests/condenser.live.ts`, `bun run tests/openai-route.live.ts`
- **Daemon restart:** `systemctl --user restart pi-context-router.service`
- **Health check:** `curl -fsS http://127.0.0.1:4141/health`

## Workflow & Verification

- Before committing changes to `main.ts`, `condenser.ts`, `utils.ts`, or `settings.ts`, run `bun run check` and `bun test tests/compression.test.ts tests/condenser.test.ts tests/persistence.test.ts`.
- When updating frontend code in `frontend/src/`, always run `bun run build` and restart the user service.
- Follow global agent rules in `~/.pi/agent/AGENTS.md` and runtime stack notes in `~/.pi/agent/AGENTS-RSI.md`.
