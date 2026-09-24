# ctx-engine

Pi context router: a localhost reverse proxy that condenses settled transcript
blocks before forwarding to a provider, plus a React dashboard.

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
```

## Run

```sh
python3 backend/ctx_engine_server.py            # binds 127.0.0.1:4141
curl -fsS http://127.0.0.1:4141/health
```

Deployed as the user unit `pi-context-router.service`
(`ExecStart=/usr/bin/python3 /home/ubuntu/src/ctx-engine/backend/ctx_engine_server.py`).
State lives in `~/.pi/agent/ctx-engine.db`; no credentials are stored in this repo.
