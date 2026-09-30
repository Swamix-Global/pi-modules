import assert from "node:assert/strict";

// Explicit live check: real inference via existing credentials, isolated cache/history.
process.env.PI_CONTEXT_ROUTER_DB = ":memory:";
const { S } = await import("../settings.ts");
const { compressBody, db } = await import("../main.ts");
const { tokens } = await import("../utils.ts");
const original = {
  messages: [
    {
      role: "user",
      content: "Record the restart procedure for pi-context-router.service.",
    },
    {
      role: "assistant",
      content:
        "Run bun run build, then systemctl --user restart pi-context-router.service, then curl -fsS http://127.0.0.1:4141/health. Verify health before reporting deployment success.\n".repeat(
          20,
        ),
    },
    { role: "user", content: "Keep this recent turn intact." },
    { role: "assistant", content: "Recent answer." },
    { role: "user", content: "Live query." },
  ],
};
const failures: unknown[] = [];
try {
  for (const model of ["gpt-6-luna", "deepseek-flash"] as const) {
    try {
      const config = {
        ...S,
        model,
        tokens: 600,
        keepTurns: 2,
        words: 60,
        maxTokens: 2048,
      };
      assert(tokens(original) > config.tokens);
      const changes: any[] = [];
      const start = Date.now();
      const result = await compressBody(original, changes, config);
      assert.equal(result.changed, true);
      assert.equal(changes.length, 1);
      assert.equal(
        changes[0].kind,
        "block",
        `${model} failed inference and dropped the turn`,
      );
      assert.equal(changes[0].cached, false);
      assert.equal(changes[0].fallback, false);
      assert.match(result.body.messages[0].content, /pi-context-router/);
      assert.deepEqual(
        result.body.messages.slice(-3),
        original.messages.slice(-3),
      );
      const repeatChanges: any[] = [];
      assert.deepEqual(
        (await compressBody(original, repeatChanges, config)).body,
        result.body,
      );
      assert.equal(repeatChanges[0].cached, true);
      console.log(
        `${model}: real summary and cache reuse passed (${Date.now() - start}ms)`,
      );
    } catch (error) {
      failures.push(error);
      console.error(`${model}: live check FAILED`);
    }
  }
  if (failures.length)
    throw new AggregateError(failures, "Live condenser verification failed");
} finally {
  db.close();
}
