import assert from "node:assert/strict";
import { ModelRuntime } from "/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js";

// Explicit live check: existing Pi credentials/config, deployed router, real inference.
const base = "http://127.0.0.1:4141";
async function history() {
  const response = await fetch(
    `${base}/api/history?search=/r/openai/responses&limit=100`,
  );
  assert.equal(response.status, 200);
  return (await response.json()) as {
    items: { id: number; route: string; status: number }[];
  };
}
const before = (await history()).items[0]?.id ?? 0;
const signal = AbortSignal.timeout(10_000);
const runtime = await ModelRuntime.create({ refreshOnCreate: false, signal });
const model = runtime.getModel("openai", "gpt-6-luna");
assert(model, "OpenAI Luna 6 missing from Pi catalog");
assert.equal(model.baseUrl, `${base}/r/openai`, "OpenAI bypasses ctx-router");
const result = await runtime.completeSimple(
  model,
  {
    systemPrompt: "Reply exactly OK.",
    messages: [
      {
        role: "user",
        content: "Verify this connection.",
        timestamp: Date.now(),
      },
    ],
  },
  { reasoning: "low", maxTokens: 256, transport: "sse", maxRetries: 0, signal },
);
assert.equal(
  result.stopReason,
  "stop",
  "Native OpenAI subscription inference failed",
);
assert.equal(
  result.content
    .filter((part) => part.type === "text")
    .map((part) => part.text)
    .join("")
    .trim(),
  "OK",
);
const row = (await history()).items.find(
  (item) => item.id > before && item.route === "POST /r/openai/responses",
);
assert(row, "Native OpenAI request not recorded by ctx-router");
assert.equal(row.status, 200);
console.log(
  "Native OpenAI subscription → ctx-router → /v1/responses: OK, persisted history verified",
);
