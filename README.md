# ctx-engine

Pi context router: a localhost reverse proxy that condenses settled transcript
blocks before forwarding to a provider, plus a React dashboard. Condensed block
length is a 400-word default sizing hint (not a hard cap), adjustable through the
50/100/200/400-word dashboard control.

On first startup after the token-to-word change, the backend archives existing
summary notes and the old `summary_tokens` setting in `ctx-engine.db`; notes are
not deleted or reused, and the new 400-word default is used unless a
`summary_words` setting is saved.

- `backend/` — stdlib-only Python HTTP server (`ctx_engine_server.py` runtime,
  `ctx_engine_utils.py` config/cost/blocks), tests in `backend/tests/`.
- `frontend/` — React + Vite + Tailwind + TanStack dashboard, served by the
  backend at `/dashboard` from `frontend/dist`.

## Build

```sh
cd frontend && npm install && npm run build
```

## Test

```sh
python3 backend/tests/test_context_router.py
python3 backend/tests/test_provider_map.py
python3 backend/tests/test_tenants.py
python3 backend/tests/test_events.py
python3 backend/tests/test_master_session.py
```

## Run

```sh
python3 backend/ctx_engine_server.py            # binds 127.0.0.1:4141
curl -fsS http://127.0.0.1:4141/health
```

Deployed as the user unit `pi-context-router.service`
(`ExecStart=/usr/bin/python3 /home/ubuntu/src/ctx-engine/backend/ctx_engine_server.py`).
State lives in `~/.pi/agent/ctx-engine.db`; no credentials are stored in this repo.

## Multi-tenant dashboard

The dashboard requires a login. The first database seeds one operator account — `Nikhil` / `Nikhil`
(`PI_CONTEXT_ROUTER_ADMIN`, `PI_CONTEXT_ROUTER_ADMIN_PASSWORD` change that before first start); the login
screen's **Create account** button (`POST /api/register`) adds more. Every account gets its own request
ledger, counters, compaction threshold, note length and condenser choice — settings never leak between
tenants, and only the operator sees the Debugger / Token Stats tabs and the `/debug/*` endpoints.

- Proxy traffic: point one tenant's Pi at `/t/<api-token>/...` (the token is on its dashboard, *Proxy path*).
  Bare `/r/...` and `/codex/responses` still work as the operator's own router while the process is local.
- `PI_CONTEXT_ROUTER_PUBLIC=1` — set behind a public reverse proxy: bare proxy paths and anonymous operator
  calls then return 401, so only sessions and tenant tokens get through. Terminate TLS in front and forward
  to `127.0.0.1:4141` (the process itself only ever binds loopback).
- `PI_CONTEXT_ROUTER_REQUIRE_LOGIN=0` — login off entirely; tests and local tooling only.
- Sessions are HttpOnly cookies for 30 days; `POST /api/logout` ends one. Failed logins are limited to ten
  attempts per name every five minutes.
- **Push instead of polling.** `GET /api/events` is a Server-Sent Events stream on the same session cookie:
  any change to that tenant's counters, request ledger or settings emits one `change` frame, and the tab
  refetches `/stats` on it (bursts collapse to one refetch per 400 ms). A comment heartbeat every 15 s keeps
  an idle channel open, and a channel that drops — router restarted, session expired — falls back to the old
  5-second poll until it reconnects. The frame carries no data, only "something changed", so the reads stay
  tenant-scoped and two accounts can never swap caches.
- **Remember me.** Sign-in stores the *username* in browser localStorage and pre-fills it next time; the
  session cookie is what keeps the account signed in for 30 days. The password itself is never written to
  browser storage — it would be readable by anything that can read that origin's localStorage.
- `POST /api/logout` also clears every cached read in the tab, so the next account to sign in on the same
  screen starts from its own data.

## Project master

```sh
python3 backend/master_session.py /absolute/project/path                  # raw snapshot + unpaid estimate
python3 backend/master_session.py /absolute/project/path --run --limit 10 # checkpoint ten API calls
python3 backend/master_session.py /absolute/project/path --run            # resume to completion
pi --session ~/.pi/agent/masters/--absolute-project-path--/mastersession.jsonl
```

`master-raw.jsonl` archives all source journal entries; `master.json` checkpoints
condensed notes; `mastersession.jsonl` is a Pi-readable projection, published
atomically **only after all blocks finish**. Output defaults to
`~/.pi/agent/masters/--<encoded-project-path>--/` (override with `--output`).
The raw snapshot preserves inactive branches; the projection follows each
session's active branch and treats native compactions as already condensed.
Rerunning skips saved notes, including `<compacted>` traces; a changed session
adds only changed blocks. The output is outside the live Pi session store; open
it explicitly, and do not overwrite a master while Pi is writing to it.

The loopback-only `POST /master/condense` takes `{"block":[...]}`, uses the
router's configured prompt and selected condenser, and returns
`{"id":"…","note":"<compacted>\\n…","prompt_hash":"…"}`. It can bill the
selected provider. A flat master can exceed the context budget: the router
will discard oldest notes from the model request, while both on-disk artifacts
remain intact.
