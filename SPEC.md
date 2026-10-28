# ctx-engine spec

Stateless proxy that rewrites chat-completion bodies so prompts stay small and
byte-stable. Port 4141, `systemd --user` unit `pi-context-router.service`.

## Decision: N-1 tool stripping, unconditional
For every request, regardless of size or provider:
1. strip media blocks (image / image_url / input_image / file / audio) from
   message history;
2. keep the newest complete tool pair before the current turn;
3. drop all older complete pairs;
4. never cut prose; never touch the current turn's messages;
5. repair dangling tool_call / tool_result entries so providers accept the body.

No token budget, no estimation, no thresholds, no per-request state. Requests
with nothing to strip are forwarded byte-identical. Design intent: identical
transcript → identical prompt bytes on every request, so provider prompt caches
stay warm and cache misses happen only when history genuinely changes.

## Wire & auth
- Routes: `/r/{provider}/{path}`, `/sglang/{path}`, `/codex/{path}`;
  `/health`, `/stats`, `/` (self-description).
- Hop-by-hop headers stripped; router injects bearer auth from
  `~/.pi/agent/auth.json` for managed providers; private `x-pi-*` headers
  never reach upstream.
- Optional gzip for cloud providers (>= 32 KiB bodies).
- Streaming responses chained untouched; provider usage recorded into
  `usage_tokens` when the body carries a usage snapshot.

## Telemetry
`/stats`: `requests`, `unchanged`, `stripped`, `pairs_dropped`, `media_removed`,
`usage_tokens`, `last_request` (`trigger`, `pairs_dropped`, `media_removed`,
`messages_before`/`after`, `model`), HTTP `status` counts, `active`, served
model. The `x-pi-router` response header tags the trigger per request.
Estimates are never reported as billed tokens; `cacheRead` truth comes from
provider usage rows.

## Config (environment, read at import; restart to change)
`PI_ROUTER_HOST`/`PI_ROUTER_PORT` (127.0.0.1:4141), `PI_ROUTER_MAX_BODY_BYTES`
(128 MiB), `SG_LANG_BASE`/`SG_LANG_MODEL`, `PI_ROUTER_AUTH`, gzip knobs,
`PI_ROUTER_CODEX_UPSTREAM`. Nothing else is tunable; there is deliberately no
budget knob.

## Files
`backend/{app,config,utils}.py` + `main.py`. Exactly these; no additions.

## Verification
Manual telemetry only — `scripts/manual_rpc.py` against a real Pi RPC instance
with real turns and real provider usage. No unit tests exist or may be added.
