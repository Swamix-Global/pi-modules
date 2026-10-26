# Project Agent Instructions: ctx-engine

Loopback context minifier: Python/FastAPI/requests in `backend/`, one thin `main.py` entry point.
No database, no dashboard API, no Svelte service — the v2 SQLite/dashboard stack is retired and
`frontend/` is dead weight kept only for history.

## Layout

- `backend/config.py`: every tunable. Environment only, read once at import; a restart is the only
  way to change behaviour. Keep new knobs here, never as scattered `os.getenv` calls.
- `backend/transport.py`: `_pi_ctx` snapshot split, auth injection, hop-header hygiene, opt-in
  request gzip. Moves bytes and credentials only; never inspects conversation content.
- `backend/strategy.py`: turn anatomy (`_blocks`, `_anchor`, `_turn_start`), media stripping,
  tool-pair repair, the pinned `Frontier`/`cut_prefix` L1 pass, whole-turn `context_messages`.
- `backend/condenser.py`: L2 folding through local SGLang granite, `local_ai` with durable file
  cache keyed by input hash, wire-format rendering.
- `backend/app.py`: FastAPI surface, `minify` pipeline, counters and pydantic shapes.
- `main.py`: `from backend.app import app, serve`; systemd runs `python main.py`.

## Invariants

- **A request that needs no change is forwarded with its original bytes.** Re-serializing the JSON
  rewrites the prompt prefix and invalidates the provider cache even when the text is identical.
- **L1 cuts one contiguous prefix of whole tool blocks before the current turn**, down to
  `budget × CUT_FRACTION`, and pins the resulting drop point per conversation. Never cut scattered
  blocks out of the middle: that shifts the head on every request and re-bills the tail.
- **The frontier moves only when the payload re-crosses the budget.** Hysteresis exists so the
  frontier moves once per band, not once per request; `STATS.trims`/`rebuild_tokens` measure the
  cost of every move, and `STATS.frontier_pins`/`frontier_resets` expose pin health.
- **The client usage snapshot never drives trimming.** It counts history from before the router's
  own cut, so it stays above the budget forever; it is telemetry and the trigger, not a measure of
  what is being sent now.
- **A pin is verified by its first kept message, never by the last dropped one.** Clients rewrite
  old history in place (bounded tool results, truncation markers); validating the dropped side
  reset the pin every request and re-cut the whole prefix, so the check follows the kept head.
- **Tool calls and results travel together.** Any cut is block-aligned and `repair_tool_pairs`
  runs after it, because providers reject an orphaned `tool` message.
- **Media before the current turn is stripped**; media in the current turn is preserved untouched.
- **The condenser is advisory.** Retries are bounded, notes are cached by input hash, and any
  failure ships the turn untouched rather than failing the request.
- **Managed providers get router-injected bearer auth** from `auth.json`; client `api-key` /
  `authorization` values are dropped for those providers and kept for pass-through ones.
- Request gzip stays opt-in (`PI_ROUTER_GZIP_PROVIDERS`): cloud chat APIs reject
  `Content-Encoding` on POST, and local uvicorn hosts cannot decode it.

## Checks and deploy

```sh
.venv/bin/python -m pytest tests -q          # 40 tests
systemctl --user restart pi-context-router.service
curl -s 127.0.0.1:4141/health                # {"ok":true,...}
curl -s 127.0.0.1:4141/stats                 # trims/rebuild_tokens should stay flat per band
```

After any change to `strategy.py` or `app.py`, confirm on live `/stats` that `trims` does not grow
per request — growing `trims` with flat request count is the cache-churn regression. A growing
`frontier_resets` means the client is rewriting the kept head itself; that is a client problem, but
it will show up as extra trims here.
