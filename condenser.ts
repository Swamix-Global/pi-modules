import { readFileSync } from "node:fs";
import { ModelRuntime } from "/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js";
import { S } from "./settings.ts";
import { json } from "./utils.ts";

export const ATTEMPT_TIMEOUT_MS = 10_000;
export const MAX_ATTEMPTS = 3;
export const lengthInstruction = (words: number) =>
  `Complete the note at approximately ${words} words; preserve important evidence, failures, constraints and unfinished work over the length target.`;

export async function condense(
  input: string,
  config: typeof S,
  signal: AbortSignal,
) {
  const systemPrompt = `${config.blockPrompt}\n\n${lengthInstruction(config.words)}`;
  let answer: unknown;
  if (config.model === "gpt-6-luna") {
    // Use Pi's existing OAuth subscription and direct upstream, never the router.
    const runtime = await ModelRuntime.create({
      authPath: config.auth || undefined,
      modelsPath: null,
      refreshOnCreate: false,
      signal,
    });
    const model = runtime.getModel("openai", config.model);
    if (!model) throw new Error("OpenAI Luna 6 is missing from Pi's catalog");
    const result = await runtime.completeSimple(
      model,
      {
        systemPrompt,
        messages: [{ role: "user", content: input, timestamp: Date.now() }],
      },
      {
        maxTokens: config.maxTokens,
        reasoning: "low",
        transport: "sse",
        maxRetries: 0,
        signal,
      },
    );
    if (!["stop", "length"].includes(result.stopReason))
      throw new Error("Codex condenser failed");
    answer = result.content
      .filter((part) => part.type === "text")
      .map((part) => part.text)
      .join("\n");
  } else {
    const auth =
      !config.apiKey && config.auth
        ? JSON.parse(readFileSync(config.auth, "utf8")).deepseek
        : null;
    const apiKey = config.apiKey || auth?.key || auth?.apiKey || "";
    const result = await fetch(config.url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(apiKey ? { Authorization: `Bearer ${apiKey}` } : {}),
      },
      body: json({
        model: config.model,
        messages: [
          { role: "system", content: systemPrompt },
          { role: "user", content: input },
        ],
        max_tokens: config.maxTokens,
        thinking: { type: "enabled" },
        reasoning_effort: "low",
      }),
      signal,
    });
    if (!result.ok) throw new Error(`Condenser HTTP ${result.status}`);
    const payload = (await result.json()) as any;
    answer = payload.choices?.[0]?.message?.content;
  }
  if (typeof answer !== "string") throw new Error("Invalid condenser output");
  const output = answer.replace(/<think>[\s\S]*?<\/think>/gi, "").trim();
  if (!output) throw new Error("Empty condenser output");
  return output;
}
