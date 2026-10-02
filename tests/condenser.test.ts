import { test, expect, spyOn } from "bun:test";
import { ModelRuntime } from "/usr/lib/node_modules/@earendil-works/pi-coding-agent/dist/index.js";
import {
  condense,
  lengthInstruction,
  summaryTargetTokens,
} from "../condenser.ts";

test("summary target is 100 tokens plus one tenth of the estimated transcript", () => {
  expect(summaryTargetTokens("x".repeat(3500))).toBe(200);
  expect(summaryTargetTokens("x".repeat(35000))).toBe(1100);
  expect(summaryTargetTokens("x".repeat(35))).toBe(101);
  expect(summaryTargetTokens("😀".repeat(3500))).toBe(200);
  expect(summaryTargetTokens("")).toBe(100);
  expect(lengthInstruction(200)).toContain("approximately 200 tokens");
});
import { S } from "../settings.ts";

test("Codex uses native Pi subscription auth, direct catalog model, no tools or internal retries", async () => {
  const options: any[] = [];
  const model = {
    id: "gpt-6-luna",
    provider: "openai",
    baseUrl: "https://api.openai.com/v1",
  };
  const create = spyOn(ModelRuntime, "create").mockImplementation(
    async (config) => {
      options.push(config);
      return {
        getModel(provider: string, id: string) {
          expect(provider).toBe("openai");
          expect(id).toBe("gpt-6-luna");
          return model;
        },
        async completeSimple(
          selected: unknown,
          context: unknown,
          settings: unknown,
        ) {
          options.push(selected, context, settings);
          return {
            stopReason: "stop",
            content: [{ type: "text", text: "Native subscription note." }],
          };
        },
      } as unknown as ModelRuntime;
    },
  );
  const controller = new AbortController();
  try {
    const config = {
      ...S,
      auth: "/existing/pi/auth.json",
      model: "gpt-6-luna" as const,
      words: 123,
      maxTokens: 2048,
      blockPrompt: "Preserve evidence.",
    };
    expect(await condense("Full turn text", config, controller.signal)).toBe(
      "Native subscription note.",
    );
    expect(options[0]).toEqual({
      authPath: config.auth,
      modelsPath: null,
      refreshOnCreate: false,
      signal: controller.signal,
    });
    expect(options[1]).toBe(model);
    expect(options[2]).toMatchObject({
      systemPrompt: `Preserve evidence.\n\n${lengthInstruction(summaryTargetTokens("Full turn text"))}`,
      messages: [{ role: "user", content: "Full turn text" }],
    });
    expect(options[2]).not.toHaveProperty("tools");
    expect(options[3]).toEqual({
      maxTokens: 2048,
      reasoning: "low",
      transport: "sse",
      maxRetries: 0,
      signal: controller.signal,
    });
  } finally {
    create.mockRestore();
  }
});
