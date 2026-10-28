# ctx-engine development instructions

Loopback context router on port 4141. Canonical source is this repository, not deployed Pi bundles.

## Ownership and layout

The backend contains **exactly three source files**. Do not split helpers into new modules or add compatibility shims or an `__init__.py`; Python namespace-package imports are sufficient.

- `backend/app.py`: HTTP API, request orchestration, effective model budgets, nonblocking proxy streaming, numeric telemetry.
- `backend/config.py`: all environment-only tunables, read once at startup.
- `backend/utils.py`: wire metadata/auth/compression; message sizing and complete tool pairs; bounded local summaries; durable session/model frontiers and cache state. Organize these responsibilities into labelled sections within this file.
- `scripts/manual_rpc.py`: manual live Pi RPC driver; not a test suite or mock.

## Invariants

1. An unchanged chat request is forwarded byte-for-byte (except the private snapshot trailer, if present). JSON whitespace itself is not provider prefix-cache content; actual message/tool tokens are.
2. Never cut merely because input exceeds the low watermark. Trigger at the full effective message budget, then cut toward 60% to create headroom.
3. Preserve user/system instructions. Remove complete call/result groups oldest-first; protect the newest current-turn group. No tool-free whole-turn deletion fallback.
4. Apply prior removals BEFORE sizing. Removed groups must not resurrect when raw history is reconstructed. Reapply the exact saved summary on every subsequent request; do not substitute original history on a cache hit.
5. Namespace state by explicit Pi session, provider, model, model limits, and initial user anchor. Summary source fingerprints must match; compaction/forks/edits must never inject stale notes. State is bounded and written atomically with mode 0600 only on frontier advances.
6. Routed chat requests have ONE aggregate budget owner: the router. `util-compaction` supplies private session/window/reserve headers and still caps tool outputs. Unrouted/unsupported transports keep the client aggregate guard. Strip all private context headers before forwarding upstream.
7. Account for tool schemas, framing and output reserve. Missing model metadata uses the operator ceiling, not a guessed model window. Do not ship a managed request known to exceed the safe budget.
8. L1 is cheap tool-pair removal; L2 handles remaining prose pressure. L2 failures are advisory and rate-limited; try older completed current-turn groups before reporting an unfit request. Never silently delete user instructions.
9. Blocking HTTP, condenser work, and upstream stream iteration must not block the ASGI event loop. `/health` reports router liveness without waiting on the condenser.
10. Prompt estimates are NOT actual provider/cache/billed tokens. `trims` counts frontier advances, not reapplications. Do not label accumulated estimates as cache-miss measurements.

## Verification policy — manual telemetry ONLY

For this subsystem and the util-compaction context guard, do not create, retain, or run unit tests, mock providers, pytest suites, or unit-test commit hooks. Retired unit tests have been removed at the operator's request.

Launch an actual Pi instance in RPC mode with the deployed extension. Send the minimum essential turns, consume RPC continuously, and wait for `agent_settled` (not `agent_end`). Record only numeric usage (`input`, `cacheRead`, `output`), context limits, frontier advance/reuse counts, and command outcomes. Never log prompts, credentials, full tool output, or entire provider streams. A bounded real diagnostic file may be used to create context pressure.

Verify: below-budget turns do not drop; one real overflow advances the frontier; subsequent small turns reuse the pinned representation and restore provider cache reads; tool calls remain valid; independent sessions/models are isolated; cached summaries are reapplied; reload/restart preserves a cut; oversize user-only input fails clearly without deletion. Label any unexercised path explicitly rather than claiming it passed. Static syntax/build checks are allowed, but are not a substitute for live telemetry.

## Deploy

Restart `systemctl --user restart pi-context-router.service`. Inspect `/health`, `/stats`, and live Pi usage. Build/deploy only the changed `util-compaction` package when other extensions have unrelated working changes. Use `/reload` in existing interactive Pi sessions; `/context-reload` is the equivalent extension command for an RPC session. A newly launched Pi loads deployed code immediately. Reloading a separate RPC instance does not reload another interactive instance.
