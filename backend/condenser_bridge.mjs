#!/usr/bin/env node
/* Isolated condenser bridge. It owns Pi SDK discovery/OAuth and makes one completion only. */
import { readFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";

const MAX_INPUT_BYTES = 8 * 1024 * 1024;
const MAX_OUTPUT_CHARS = 256 * 1024;
const CODEX_PROVIDER = "openai-codex";
const CODEX_MODEL = "gpt-6-luna";
const PI_ENTRY = "/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js";
const LOCAL_AI = "/home/ubuntu/.pi/agent/lib/local-ai.mjs";

function fail(message) {
  process.stdout.write(JSON.stringify({ ok: false, error: message }));
  process.exitCode = 1;
}

async function readRequest() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_INPUT_BYTES) throw new Error("bridge request too large");
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || typeof value !== "object" || typeof value.action !== "string") throw new Error("invalid bridge request");
  return value;
}

async function runtime() {
  const { ModelRuntime } = await import(PI_ENTRY);
  return ModelRuntime.create({ modelsPath: null, refreshOnCreate: false });
}

async function discover() {
  const models = await runtime();
  const registered = models.getModel(CODEX_PROVIDER, CODEX_MODEL);
  if (!registered) return { ok: true, available: false, provider: CODEX_PROVIDER, model: CODEX_MODEL, error: "Codex Luna model is not registered" };
  const available = await models.getAvailable(CODEX_PROVIDER, { signal: AbortSignal.timeout(10000) });
  const model = available.find((item) => item.provider === CODEX_PROVIDER && item.id === CODEX_MODEL);
  return model
    ? { ok: true, available: true, provider: model.provider, model: model.id }
    : { ok: true, available: false, provider: CODEX_PROVIDER, model: CODEX_MODEL, error: "Codex Luna authentication is unavailable" };
}

function textFrom(message) {
  return (message?.content ?? [])
    .filter((part) => part && part.type === "text")
    .map((part) => String(part.text ?? ""))
    .join("")
    .trim();
}

async function complete(request) {
  if (typeof request.prompt !== "string" || typeof request.system !== "string") throw new Error("invalid completion request");
  if (request.prompt.length > MAX_INPUT_BYTES || request.system.length > MAX_INPUT_BYTES) throw new Error("completion request too large");
  const models = await runtime();
  const model = models.getModel(CODEX_PROVIDER, CODEX_MODEL);
  if (!model) throw new Error("Codex Luna model is not registered");
  const available = await models.getAvailable(CODEX_PROVIDER, { signal: AbortSignal.timeout(10000) });
  if (!available.some((item) => item.provider === CODEX_PROVIDER && item.id === CODEX_MODEL)) throw new Error("Codex Luna authentication is unavailable");
  const timeoutMs = Number.isFinite(request.timeoutMs) ? Math.max(1000, Math.min(60000, Math.floor(request.timeoutMs))) : 60000;
  const signal = AbortSignal.timeout(timeoutMs);
  const message = await models.completeSimple(
    model,
    { systemPrompt: request.system, messages: [{ role: "user", content: request.prompt, timestamp: Date.now() }] },
    { reasoning: "off", maxTokens: 4096, timeoutMs, maxRetries: 0, signal },
  );
  const text = textFrom(message);
  if (!text) throw new Error("Codex Luna returned no usable text");
  if (text.length > MAX_OUTPUT_CHARS) throw new Error("Codex Luna response too large");
  return { ok: true, provider: message.provider, model: message.model, text, usage: message.usage ?? null };
}

async function local(request) {
  if (typeof request.prompt !== "string" || typeof request.system !== "string") throw new Error("invalid completion request");
  if (request.prompt.length > MAX_INPUT_BYTES || request.system.length > MAX_INPUT_BYTES) throw new Error("completion request too large");
  const { LocalAI } = await import(pathToFileURL(LOCAL_AI).href);
  const timeoutMs = Number.isFinite(request.timeoutMs) ? Math.max(1000, Math.min(60000, Math.floor(request.timeoutMs))) : 60000;
  const text = await LocalAI(request.prompt, {
    system: request.system,
    thinking: false,
    maxTokens: 4096,
    timeoutMs,
    signal: AbortSignal.timeout(timeoutMs),
    source: "ctx-engine",
    model: typeof request.model === "string" ? request.model : undefined,
  });
  if (!text) throw new Error("local condenser returned no usable text");
  if (text.length > MAX_OUTPUT_CHARS) throw new Error("local condenser response too large");
  return { ok: true, provider: "local", model: typeof request.model === "string" ? request.model : "", text };
}

try {
  const request = await readRequest();
  const result = request.action === "discover" ? await discover()
    : request.action === "complete-codex" ? await complete(request)
      : request.action === "complete-local" ? await local(request)
        : (() => { throw new Error("unknown bridge action"); })();
  const encoded = JSON.stringify(result);
  if (encoded.length > MAX_OUTPUT_CHARS) throw new Error("bridge response too large");
  process.stdout.write(encoded);
} catch (error) {
  fail(error instanceof Error ? error.message : "bridge failed");
}
