# ctx-engine

Loopback context minifier for Pi at **http://127.0.0.1:4141**.

It sits between Pi and every inference host, shrinks the body it forwards, and — the part that
actually decides cost — keeps the surviving prefix **byte-identical** between requests so the
host keeps serving the tail from its prompt cache. A cut that lands in the middle of history
invalidates that cache and re-bills the whole prompt as fresh input; that failure mode is what
this service is built to avoid.

## Layout

```text
backend/
  config.py      every knob, read once from the environment
  transport.py   _pi_ctx snapshot split, auth, hop headers, opt-in request gzip
  strategy.py    turn anatomy, pinned L1 drop frontier, whole-turn trim
  condenser.py   L2: local SGLang granite folding, durable file cache
  app.py         FastAPI surface, pipeline, counters, shapes
main.py          entry point (`python main.py`); systemd ExecStart runs this
tests/           test_strategy.py, test_providers.py, test_transport.py, test_condenser.py
frontend/        Svelte dashboard from the retired v2 build; not served by this service
pi-context-router.service   unit template (installed copy lives in ~/.config/systemd/user)
```

## Setup and run

Requires Python 3.13+; the virtual environment lives in `.venv/`.

```sh
uv venv .venv --python python3
uv pip install --python .venv/bin/python fastapi uvicorn requests pydantic
.venv/bin/python main.py                                  # foreground
systemctl --user restart pi-context-router.service        # deployed way
curl -s 127.0.0.1:4141/health
```

## What happens to one request

1. **Snapshot split.** Pi appends `_pi_ctx::{"usage": …}` to the body. Usage comes from the
   host, never from the estimate; the marker is stripped before forwarding.
2. **L1 — pinned frontier.** Above `PI_ROUTER_BUDGET` usage, whole tool blocks before the
   current turn are dropped as one contiguous prefix, down to `budget × PI_ROUTER_CUT_FRACTION`.
   The drop point is remembered per conversation and only moves when the payload re-crosses the
   budget, so the head stays cached for many turns. Hysteresis is the whole point: cutting
   exactly to the budget re-crosses it within a turn or two and rebuilds the cache every request.
3. **L2 — condense.** If L1 alone did not fit, pre-turn history is folded into one leading note
   through the local granite model (durable cache keyed by input hash, fail-open: a dead
   condenser ships the turn untouched).
4. **Verbatim forward.** A request that needed nothing is forwarded with its original bytes —
   re-serializing JSON changes bytes and breaks the cache even when the text is "the same".

## Endpoints

| path | purpose |
|---|---|
| `/r/{provider}/{rest}` | provider pass-through with minification |
| `/v1/chat/completions` | local SGLang pass-through, never minified (it is the condenser) |
| `/codex/responses` | Codex responses forwarded verbatim to the ChatGPT backend |
| `/health` | condenser liveness + lifetime counters |
| `/stats` | every counter the pipeline mutates |
| `/` | mode, budget, strategy, endpoint list |

## Configuration

All environment-driven, read once at import; restart to change. Defaults in parentheses.

| variable | meaning |
|---|---|
| `PI_ROUTER_BUDGET` (60000) | usage that triggers minification |
| `PI_ROUTER_CUT_FRACTION` (0.4) | hysteresis: L1 cuts down to this share of the budget |
| `PI_ROUTER_LEVEL` (L2) | `L1` drop-only, `L2` drop + fold |
| `PI_ROUTER_PORT` / `PI_ROUTER_HOST` (4141 / 127.0.0.1) | bind address |
| `PI_ROUTER_MAX_BODY_BYTES` (128 MiB) | larger bodies pass through untouched |
| `PI_ROUTER_AUTH` (`~/.pi/agent/auth.json`) | provider bearer keys the router injects |
| `PI_ROUTER_CACHE` (`~/.pi/agent/ctx-cache`) | condenser note cache |
| `PI_ROUTER_ROUTES` | provider → base URL JSON override |
| `PI_ROUTER_GZIP_PROVIDERS` (empty) | comma list that accepts gzipped request bodies |
| `PI_ROUTER_CODEX_UPSTREAM` | Codex backend URL |
| `SG_LANG_BASE` / `SG_LANG_MODEL` | local condenser host and model override |

`frontend/` is a leftover from the retired v2 dashboard (SQLite request log, settings API); this
service serves no UI, and the old `PI_CONTEXT_ROUTER_*` variables and `.env` are gone. Config is
process environment only.

## Why the frontier is pinned (measured)

Session telemetry from the pre-pinning router: `cacheRead` sat at the system prompt alone
(median 11,520 tokens) on 254 of 448 cut requests, and 288 of 508 turns hit below 80 % cache —
the head was being rewritten constantly while the tail was re-billed as fresh input. Simulation of
the old "cut exactly to budget" pass moved the drop frontier on **74 %** of turns; a pinned
frontier with hysteresis moves it on ~4 %.

## Checks

```sh
.venv/bin/python -m pytest tests -q     # 40 tests: strategy, wire, condenser, full pipeline
```

`test_providers.py` drives the real ASGI app with fake upstreams: deepseek-style tool chains,
OpenAI encrypted reasoning, antigravity signed thinking, streaming, snapshot-free clients, the
condenser path, fail-open behavior, and frontier pinning across requests.
