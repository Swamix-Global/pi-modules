# Pi context engine

Loopback context router at `127.0.0.1:4141`. It reduces old tool history while preserving user/system instructions and amortizing unavoidable provider prefix-cache breaks.

## Backend layout — exactly three files

| File | Responsibility |
|---|---|
| `backend/app.py` | HTTP API, effective budgets, request orchestration, streaming and telemetry |
| `backend/config.py` | Centralized configuration and environment tunables |
| `backend/utils.py` | Wire/auth helpers, message fitting, local summaries and durable frontier state |

No extra backend modules, compatibility wrappers, or `__init__.py`. `main.py` remains the thin service entry point and imports the namespace package. Keep new helpers in the appropriate existing file.

## How fitting works

1. `util-compaction` bounds tool output once, at creation. For managed loopback `/r/` chat routes it sends private session/window/reserve headers; the router alone owns aggregate fitting. Unsupported/unrouted models retain the client guard.
2. The router caps its 60,000-token operator ceiling by the selected model window, output reserve, tool schemas, and framing allowance. Sizes are conservative character-based **estimates**, not provider token counts.
3. Reapply the session/provider/model frontier before sizing. Below the full budget, do not cut anything new. On overflow, remove complete oldest tool pairs toward 60% of the budget. Preserve user/system prose and the newest current-turn batch.
4. If tool removal cannot relieve prose pressure, L2 asks the local SGLang model for bounded notes. Save the exact replacement and source fingerprints; reuse it on later requests rather than resurrecting original history. Condenser failures are advisory and rate-limited.
5. An unfit request fails with a clear context error, not silent deletion of user instructions. Pi's transcript remains intact.

Frontier metadata (IDs, source hashes, optional notes; no raw prompts) lives under `~/.pi/agent/ctx-cache/frontiers/`, written atomically with mode 0600. The cache is bounded to 128 conversations, with a 24-hour idle TTL. Source edits/compaction cannot reuse stale notes. Concurrent sessions/models are isolated. A router restart reloads saved cuts on demand.

Cutting a tool group changes the provider token prefix from that point onward. Hysteresis limits how often this happens; it cannot eliminate that initial miss. JSON whitespace changes alone are not evidence of a provider cache miss.

## API

| Endpoint | Purpose |
|---|---|
| `GET /health` | Router liveness plus last-known condenser identity; never waits for the condenser |
| `GET /stats` | Frontier advances/reuse, guard failures, persistence errors, last request's estimated sizes |
| `GET /` | Mode, ceiling, strategy, endpoints |
| `POST /r/{provider}/{rest}` | Managed provider proxy; chat `messages` receive fitting |
| `POST /v1/chat/completions` | Local SGLang passthrough (condenser traffic is not recursively fitted) |
| `POST /codex/responses` | Codex Responses passthrough; client guard remains responsible |

Pi sends `x-pi-ctx-session`, `x-pi-ctx-window`, and `x-pi-ctx-reserve` through the supported header hook. These headers are stripped before upstream forwarding. Other wire formats pass through without chat transforms. Authentication is injected from Pi's auth store; never put credentials into telemetry. Request gzip remains opt-in.

`trims` counts **frontier advances**, not every request that reapplies a cut. `frontier_reuses` counts stable reapplication. `last_request` contains only hashed identity, model, numeric estimates, and decisions. The retired `rebuild_tokens` counter was an estimate of whole trimmed payloads, not actual re-billed input. Read actual `input`, `cacheRead`, and `output` from Pi RPC/provider usage. No access-log stream or request-body logging is needed.

## Configuration and deployment

Tunables are in `backend/config.py`, read once at startup. Main controls: `PI_ROUTER_BUDGET` (60000), `PI_ROUTER_LEVEL` (L2), `SG_LANG_BASE`, `SG_LANG_MODEL`, and `PI_ROUTER_CACHE`. The local condenser uses whichever model SGLang actually serves unless explicitly configured. Never assume the profile name is the served model.

```sh
systemctl --user restart pi-context-router.service
curl -s http://127.0.0.1:4141/health
curl -s http://127.0.0.1:4141/stats
```

Build/deploy the changed util-compaction package, then `/reload` existing interactive Pi sessions. Its `/context-reload` extension command is callable through RPC. A separate RPC process cannot reload an already-open interactive session.

## Development verification: manual live telemetry only

See `AGENTS.md`. Unit tests and their hook references for this subsystem are retired. Do not create mock-provider or unit-test substitutes.

`scripts/manual_rpc.py` starts an actual ephemeral Pi instance, consumes RPC continuously, passes supplied turns, and waits for `agent_settled`. Use the dedicated python tool to invoke it with `runpy.run_path`, setting `sys.argv` with `--provider`, `--model`, `--cwd`, and repeated `--turn` values (or a private JSON `--turns-file`). `--reload-after N` exercises the supported reload command; `--report` saves only minimal numeric telemetry at mode 0600. Calls incur real provider usage. No prompts, credentials, tool output, or full streams are logged.

Manually observe below-budget behavior, one actual pressure cut followed by cache recovery, independent session/model behavior, summary reuse where the condenser is available, and durable reapplication after restart. Record gaps honestly. Static builds/syntax checks are allowed, but do not prove live behavior.
