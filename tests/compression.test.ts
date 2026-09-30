import { test, expect, afterAll, spyOn } from "bun:test";
import { Database } from "bun:sqlite";
import { gzipSync, zstdCompressSync } from "node:zlib";
import { hash, json, tokens, turns, transcript } from "../utils.ts";
import {
  ATTEMPT_TIMEOUT_MS,
  MAX_ATTEMPTS,
  lengthInstruction,
} from "../condenser.ts";
let calls = 0,
  mode = "ok",
  recorded: Uint8Array | undefined,
  recordedUrl = "",
  prompt = "",
  systemPrompt = "",
  condenserPayload: any,
  failuresLeft = 0,
  arrivals: number[] = [],
  releases: (() => void)[] = [],
  fanoutReady: (() => void) | undefined,
  streamController: ReadableStreamDefaultController<Uint8Array> | undefined;
const upstream = Bun.serve({
  hostname: "127.0.0.1",
  port: 0,
  idleTimeout: 0,
  async fetch(request) {
    if (new URL(request.url).pathname === "/condense") {
      calls++;
      const payload = (await request.json()) as any;
      prompt = payload.messages[1].content;
      systemPrompt = payload.messages[0].content;
      condenserPayload = payload;
      arrivals.push(Date.now());
      if (mode === "fail" || failuresLeft-- > 0)
        return new Response("unavailable", { status: 503 });
      if (mode === "hang")
        return new Response(
          new ReadableStream({
            start(c) {
              c.enqueue(Buffer.from('{"choices":'));
            },
          }),
        );
      if (mode === "barrier") {
        await new Promise<void>((resolve) => {
          releases.push(resolve);
          if (releases.length === 5) fanoutReady?.();
        });
      }
      return Response.json({
        choices: [
          {
            message: {
              content: "Concise useful result with exact identifiers.",
            },
          },
        ],
      });
    }
    if (new URL(request.url).pathname === "/stream")
      return new Response(
        new ReadableStream({
          start(c) {
            streamController = c;
            c.enqueue(Buffer.from("data: first\n\n"));
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      );
    recordedUrl = request.url;
    recorded = new Uint8Array(await request.arrayBuffer());
    return new Response(recorded as Uint8Array<ArrayBuffer>, {
      headers: {
        "Content-Type": "application/octet-stream",
        "X-Upstream-Auth": request.headers.get("authorization") ?? "",
      },
    });
  },
});
Object.assign(process.env, {
  PI_CONTEXT_ROUTER_DB: ":memory:",
  PI_CONTEXT_ROUTER_AUTH: "",
  PI_CONTEXT_ROUTER_API_KEY: "test",
  PI_CONTEXT_ROUTER_CONDENSER_MODEL: "deepseek-flash",
  PI_CONTEXT_ROUTER_TOKENS: "600",
  PI_CONTEXT_ROUTER_KEEP_TURNS: "2",
  PI_CONTEXT_ROUTER_CONDENSER_URL: `${upstream.url}condense`,
  PI_CONTEXT_ROUTER_ROUTES: json({ test: upstream.url.toString() }),
  PI_CONTEXT_ROUTER_UPSTREAM: upstream.url.toString(),
  PI_CONTEXT_ROUTER_MAX_BODY_BYTES: "16384",
});
const { app, db, compressBody, initCache } = await import("../main.ts");
app.listen({ hostname: "127.0.0.1", port: 0, idleTimeout: 0 });
const base = `http://127.0.0.1:${app.server!.port}`;
afterAll(async () => {
  await app.stop(true);
  await upstream.stop(true);
  db.close();
});
const initialSettings = await (await fetch(`${base}/api/settings`)).json();

test("dashboard settings validate atomically, persist and reject cross-origin changes", async () => {
  expect(initialSettings.settings.words).toBe(400);
  expect(Object.keys(initialSettings.settings).sort()).toEqual(
    [
      "tokens",
      "keepTurns",
      "words",
      "maxTokens",
      "blockPrompt",
      "model",
    ].sort(),
  );
  expect(initialSettings.defaults.model).toBe("deepseek-flash");
  for (const patch of [
    { words: 0 },
    { apiKey: "leak" },
    { tokens: "100" },
    { keepTurns: 1.5 },
    { blockPrompt: "" },
    { model: "unknown" },
    ...[
      "temperature",
      "trimFloor",
      "workers",
      "timeout",
      "excerpt",
      "toolPrompt",
      "toolTokens",
    ].map((key) => ({ [key]: 100 })),
    null,
  ]) {
    const result = await fetch(`${base}/api/settings`, {
      method: "PUT",
      body: json(patch),
    });
    expect(result.status).toBe(400);
    expect(
      (await (await fetch(`${base}/api/settings`)).json()).settings,
    ).toEqual(initialSettings.settings);
  }
  for (const headers of [
    { Origin: "https://evil.example" },
    { "Sec-Fetch-Site": "cross-site" },
  ] as Record<string, string>[]) {
    expect(
      (
        await fetch(`${base}/api/settings`, {
          method: "PUT",
          headers,
          body: json({ words: 150 }),
        })
      ).status,
    ).toBe(403);
    expect((await fetch(`${base}/api/history`, { headers })).status).toBe(403);
  }
  const patch = { words: 120, maxTokens: 2000 };
  const updated = await fetch(`${base}/api/settings`, {
    method: "PUT",
    headers: { Origin: base },
    body: json(patch),
  });
  expect(updated.status).toBe(200);
  expect((await updated.json()).defaults).toEqual(initialSettings.defaults);
  expect(
    JSON.parse(
      (db.query("SELECT value FROM dashboard_settings WHERE id=1").get() as any)
        .value,
    ).words,
  ).toBe(120);
  const body = { messages: [{ role: "tool", content: "p".repeat(400) }] };
  const compressed = await fetch(`${base}/compress`, {
    method: "POST",
    body: json(body),
  });
  expect(compressed.headers.get("X-Context-Router")).toBe("pass");
  await fetch(`${base}/api/settings`, {
    method: "PUT",
    body: json(initialSettings.settings),
  });
});

test("full history retains originals, every compression change, cache hits and failed requests", async () => {
  const body = {
    ...longBody(),
    model: "history-unique",
    messages: [
      { role: "system", content: "Preserve instructions." },
      { role: "user", content: "history query " + "h".repeat(1300) },
      { role: "assistant", content: "history answer " + "h".repeat(1300) },
      { role: "user", content: "previous query" },
      { role: "assistant", content: "previous answer" },
      { role: "user", content: "live query" },
      {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: "c1",
            type: "function",
            function: { name: "read", arguments: '{"path":"exact"}' },
          },
        ],
      },
    ],
  };
  for (let i = 0; i < 2; i++) {
    const result = await fetch(`${base}/compress`, {
      method: "POST",
      body: json(body),
    });
    expect(result.status).toBe(200);
    const output = await result.json();
    const list = await (
      await fetch(
        `${base}/api/history?search=history-unique&changed=yes&limit=1`,
      )
    ).json();
    const detail = await (
      await fetch(`${base}/api/history/${list.items[0].id}`)
    ).json();
    expect(detail.original).toEqual(body);
    expect(detail.compressed).toEqual(output);
    expect(detail.changes.length).toBeGreaterThanOrEqual(1);
    expect(detail.changes[0]).toMatchObject({
      kind: "block",
      cached: i === 1,
      fallback: false,
    });
    expect(detail.before_tokens).toBe(tokens(body));
    expect(detail.after_tokens).toBe(tokens(output));
    expect(detail.status).toBe(200);
  }
  await fetch(`${base}/compress`, { method: "POST", body: "{not json" });
  const latest = await (await fetch(`${base}/api/history?limit=1`)).json();
  const failed = await (
    await fetch(`${base}/api/history/${latest.items[0].id}`)
  ).json();
  expect(failed.status).toBe(400);
  expect(failed.original).toBe("{not json");
  expect(failed.error).toBe("invalid JSON");
  expect(failed.compressed).toBeNull();
  expect((await fetch(`${base}/api/history/9999999`)).status).toBe(404);
});

test("history cursor paging has no duplicate or missing rows, filters and metrics agree", async () => {
  const beforeCount = (db.query("SELECT count(*) n FROM history").get() as any)
    .n;
  for (let i = 0; i < 6; i++)
    await fetch(`${base}/compress`, {
      method: "POST",
      body: json({
        model: "page-history",
        messages: [{ role: "user", content: `small ${i}` }],
      }),
    });
  let cursor: number | null = null;
  const ids: number[] = [];
  do {
    const page = await (
      await fetch(
        `${base}/api/history?limit=2&search=page-history&changed=no${cursor === null ? "" : `&before=${cursor}`}`,
      )
    ).json();
    expect(page.total).toBe(6);
    ids.push(...page.items.map((item: any) => item.id));
    cursor = page.next;
  } while (cursor !== null);
  expect(ids.length).toBe(6);
  expect(new Set(ids).size).toBe(6);
  expect(ids).toEqual([...ids].sort((a, b) => b - a));
  for (const query of ["limit=0", "limit=101", "before=bad", "changed=invalid"])
    expect((await fetch(`${base}/api/history?${query}`)).status).toBe(400);
  const stats = await (await fetch(`${base}/stats`)).json();
  expect(stats.history.requests).toBe(beforeCount + 6);
  expect(stats.history).toEqual(
    db
      .query(
        "SELECT count(*) requests,coalesce(sum(changed),0) compacted,coalesce(sum(status>=400),0) errors,coalesce(sum(before_tokens),0) before_tokens,coalesce(sum(after_tokens),0) after_tokens,coalesce(sum(duration_ms),0) duration_ms FROM history",
      )
      .get(),
  );
});

test("dashboard serves built frontend and explicit asset routes", async () => {
  const result = await fetch(`${base}/dashboard/`);
  expect(result.status).toBe(200);
  expect(result.headers.get("Content-Type")).toContain("text/html");
  const page = await result.text();
  const assets = [
    ...page.matchAll(/(?:src|href)="(\/dashboard\/assets\/[^"]+)"/g),
  ].map((match) => match[1]);
  expect(assets.length).toBeGreaterThanOrEqual(2);
  for (const asset of assets)
    expect((await fetch(`${base}${asset}`)).status).toBe(200);
  expect((await fetch(`${base}/dashboard/assets/missing.js`)).status).toBe(404);
  expect((await fetch(`${base}/dashboard/assets/invalid.exe`)).status).toBe(
    404,
  );
});

const longBody = () => ({
  model: "test",
  messages: [
    { role: "system", content: "Preserve instructions." },
    { role: "user", content: "old query " + "x".repeat(1300) },
    { role: "assistant", content: "old answer " + "y".repeat(1300) },
    { role: "user", content: "previous query" },
    { role: "assistant", content: "previous answer" },
    { role: "user", content: "live query" },
    {
      role: "assistant",
      content: null,
      tool_calls: [
        {
          id: "c1",
          type: "function",
          function: { name: "read", arguments: '{"path":"exact"}' },
        },
      ],
    },
  ],
});
test("group turns before rendering all five section types", () => {
  const items = [
    { role: "system", content: "keep" },
    { role: "user", content: "question" },
    {
      role: "assistant",
      content: "answer",
      tool_calls: [{ function: { name: "read", arguments: "{}" } }],
    },
    { role: "tool", tool_call_id: "c", content: "result" },
    { type: "reasoning", summary: [{ text: "thought" }] },
    { role: "user", content: "next" },
  ];
  const [prefix, groups] = turns(items);
  expect(prefix).toEqual([items[0]]);
  expect(groups.map((g) => g.length)).toEqual([4, 1]);
  expect(transcript(groups[0])).toBe(
    "user:\nquestion\n---\nassistant:\nanswer\n---\ntool_call:\nread({})\n---\ntool_output:\nresult\n---\nthinking:\nthought\n---\n---------------------------",
  );
  expect(
    transcript([groups[0][0], groups[0][3], groups[0][1], groups[0][2]]),
  ).toBe(transcript(groups[0]));
  expect(hash({ b: 1, a: 2 })).toBe(hash({ a: 2, b: 1 }));
});
test("blocks compress to plain content; cache and rendered hashes prevent repeat calls", async () => {
  const original = longBody(),
    frozen = json(original),
    before = calls;
  const first = await compressBody(original);
  expect(first.changed).toBe(true);
  expect(calls - before).toBe(1);
  expect(json(original)).toBe(frozen);
  expect(first.body.messages[1]).toEqual({
    role: "user",
    content: "Concise useful result with exact identifiers.",
  });
  expect(json(first.body)).not.toContain("<compacted>");
  expect(first.body.messages.slice(-4)).toEqual(original.messages.slice(-4));
  expect(tokens(first.body)).toBeLessThanOrEqual(600);
  expect(prompt).toContain("user:\nold query");
  expect(prompt).toContain("\n---\nassistant:");
  expect(prompt).not.toContain("Target approximately");
  expect(systemPrompt).toBe(
    `${initialSettings.settings.blockPrompt}\n\n${lengthInstruction(400)}`,
  );
  expect(condenserPayload).toMatchObject({
    model: "deepseek-flash",
    reasoning_effort: "low",
    thinking: { type: "enabled" },
    max_tokens: 4096,
  });
  expect(condenserPayload).not.toHaveProperty("temperature");
  await compressBody(original);
  expect(calls - before).toBe(1);
  const repeatCalls = calls;
  await compressBody({
    ...first.body,
    messages: [
      ...first.body.messages,
      { role: "user", content: "new turn" },
      { role: "assistant", content: "z".repeat(1600) },
    ],
  });
  expect(calls - repeatCalls).toBe(1); // Only the newly settled turn, never the plain cached note.
  expect(db.query("SELECT count(*) n FROM compressed").get()).toBeTruthy();
});
test("blocks retry if no cached block present as per key; hits return cached note", async () => {
  const body = longBody();
  body.messages[1].content = "different old query " + "q".repeat(1300);
  const before = calls;
  const first = await compressBody(body);
  expect(first.changed).toBe(true);
  expect(calls - before).toBe(1);
  const second = await compressBody(body);
  expect(second.body).toEqual(first.body);
  expect(calls - before).toBe(1); // Cached as per key: no repeat condenser call
});
test("tool outputs are preserved as-is under token budget", async () => {
  const before = calls;
  const content = "T".repeat(351);
  const body = {
    messages: [
      { role: "user", content: "read threshold file" },
      { role: "tool", tool_call_id: "threshold", content },
    ],
  };
  expect(tokens(body)).toBeLessThan(600);
  const result = await compressBody(body);
  expect(result.changed).toBe(false);
  expect(result.body.messages[1].content).toBe(content);
  expect(calls - before).toBe(0);
});
test("preserve live tool-call ids, arguments, outputs and images", async () => {
  const image = {
    type: "image_url",
    image_url: { url: "data:image/png;base64," + "X".repeat(3000) },
  };
  const body = {
    messages: [
      { role: "user", content: "read file" },
      {
        role: "assistant",
        content: null,
        tool_calls: [
          {
            id: "keep-id",
            function: { name: "read", arguments: '{"path":"exact"}' },
          },
        ],
      },
      {
        role: "tool",
        tool_call_id: "keep-id",
        content: [
          { type: "text", text: "large tool output " + "A".repeat(500) },
          image,
        ],
      },
    ],
  };
  const before = calls,
    result = await compressBody(body);
  expect(calls - before).toBe(0);
  expect(result.changed).toBe(false);
  expect(result.body.messages[1]).toEqual(body.messages[1]);
  expect(result.body.messages[2].tool_call_id).toBe("keep-id");
  expect(result.body.messages[2].content[1]).toEqual(image);
  expect(result.body.messages[2].content[0].text).toBe(
    "large tool output " + "A".repeat(500),
  );
});
test("Responses and Anthropic tool results retain their native wrappers", async () => {
  const body = {
    input: [
      { role: "user", content: [{ type: "input_text", text: "query" }] },
      { type: "function_call", name: "read", call_id: "keep", arguments: "{}" },
      {
        type: "function_call_output",
        call_id: "keep",
        output: "B".repeat(500),
      },
    ],
  };
  const result = await compressBody(body);
  expect(result.body.input[1]).toEqual(body.input[1]);
  expect(result.body.input[2].call_id).toBe("keep");
  expect(result.body.input[2].output).toBe("B".repeat(500));
  const anthropic = {
    messages: [
      { role: "user", content: "question" },
      {
        role: "assistant",
        content: [{ type: "tool_use", id: "same", name: "read", input: {} }],
      },
      {
        role: "user",
        content: [
          {
            type: "tool_result",
            tool_use_id: "same",
            content: "C".repeat(500),
          },
        ],
      },
    ],
  };
  expect(turns(anthropic.messages)[1].length).toBe(1);
  const resultAnthropic = await compressBody(anthropic);
  expect(resultAnthropic.body.messages[2].content[0].tool_use_id).toBe("same");
  expect(resultAnthropic.changed).toBe(false);
});
test("failed condensation leaves cache empty; retry can recover", async () => {
  const body = longBody();
  body.messages[1].content = "outage " + "Q".repeat(1300);
  mode = "fail";
  const before = calls;
  const log = spyOn(console, "error").mockImplementation(() => {});
  let failed;
  try {
    failed = await compressBody(body);
    expect(calls - before).toBe(3);
    expect(log).toHaveBeenCalledTimes(1);
    expect(log.mock.calls[0][0]).toContain(
      "CRITICAL: condenser exhausted 3 attempts (10000ms each)",
    );
    expect(log.mock.calls[0][0]).not.toContain("outage");
  } finally {
    log.mockRestore();
    mode = "ok";
  }
  expect(failed.changed).toBe(true);
  expect(tokens(failed.body)).toBeLessThanOrEqual(600);
  const count = (db.query("SELECT count(*) n FROM compressed").get() as any).n;
  mode = "ok";
  const recovered = await compressBody(body);
  expect(recovered.body.messages[1].content).toBe(
    "Concise useful result with exact identifiers.",
  );
  expect((db.query("SELECT count(*) n FROM compressed").get() as any).n).toBe(
    count + 1,
  );
});
test("fanout launches even duplicate uncached blocks and waits for the last result", async () => {
  const body = {
    messages: [
      ...Array.from({ length: 5 }, () => ({
        role: "user",
        content: "fanout identical uncached source " + "X".repeat(1300),
      })),
      { role: "user", content: "protected previous" },
      { role: "user", content: "protected live" },
    ],
  };
  const before = calls;
  releases = [];
  mode = "barrier";
  let settled = false;
  const ready = new Promise<void>((resolve) => {
    fanoutReady = resolve;
  });
  const pending = compressBody(body).then((result) => {
    settled = true;
    return result;
  });
  try {
    await ready;
    expect(calls - before).toBe(5);
    expect(settled).toBe(false);
    releases.slice(0, 4).forEach((release) => release());
    await Promise.resolve();
    expect(settled).toBe(false);
    releases[4]();
    const result = await pending;
    expect(result.body.messages.slice(-2)).toEqual(body.messages.slice(-2));
    expect(result.body.messages.length).toBe(7);
  } finally {
    releases.forEach((release) => release());
    mode = "ok";
    fanoutReady = undefined;
    await pending;
  }
});

test("cache key is message-model-outlen only; failures retry per block", async () => {
  const { S } = await import("../settings.ts");
  const body = longBody();
  body.messages[1].content = "cache-key-check " + "K".repeat(1300);
  const before = calls;
  failuresLeft = 2;
  await compressBody(body);
  expect(calls - before).toBe(3);
  const block = turns(body.messages)[1][0];
  expect(
    db
      .query("SELECT key FROM compressed WHERE key=?")
      .get(`${hash(block)}-deepseek-flash-4096`),
  ).toBeTruthy();
  await compressBody(body, [], {
    ...S,
    words: 25,
    blockPrompt: "Different sizing hint",
  });
  expect(calls - before).toBe(3);
  await compressBody(body, [], { ...S, maxTokens: 5000 });
  expect(calls - before).toBe(4);
});

test("live text is never clipped, even when it exceeds the budget", async () => {
  const original = {
    messages: [{ role: "user", content: "live-intact " + "L".repeat(5000) }],
  };
  const frozen = json(original);
  const changes: any[] = [];
  const before = calls;
  await expect(compressBody(original, changes)).rejects.toThrow(
    "protected turns or fixed request fields exceed token budget",
  );
  expect(json(original)).toBe(frozen);
  expect(changes).toEqual([]);
  expect(calls).toBe(before);
});

test("hung response bodies get three real 10-second deadlines and one critical log", async () => {
  expect(ATTEMPT_TIMEOUT_MS).toBe(10_000);
  expect(MAX_ATTEMPTS).toBe(3);
  const body = longBody();
  body.messages[1].content = "timeout-unique " + "H".repeat(1300);
  const before = calls;
  const count = (db.query("SELECT count(*) n FROM compressed").get() as any).n;
  const log = spyOn(console, "error").mockImplementation(() => {});
  arrivals = [];
  mode = "hang";
  const start = Date.now();
  try {
    const result = await compressBody(body);
    const elapsed = Date.now() - start;
    expect(calls - before).toBe(3);
    expect(elapsed).toBeGreaterThanOrEqual(29_500);
    expect(elapsed).toBeLessThan(34_000);
    for (let i = 1; i < arrivals.length; i++) {
      expect(arrivals[i] - arrivals[i - 1]).toBeGreaterThanOrEqual(9_500);
      expect(arrivals[i] - arrivals[i - 1]).toBeLessThan(12_000);
    }
    expect(log).toHaveBeenCalledTimes(1);
    expect(log.mock.calls[0][0]).toContain("CRITICAL:");
    expect((db.query("SELECT count(*) n FROM compressed").get() as any).n).toBe(
      count,
    );
    expect(result.body.messages).toEqual(
      body.messages.slice(0, 1).concat(body.messages.slice(3)),
    );
  } finally {
    mode = "ok";
    log.mockRestore();
  }
}, 40_000);

test("immutable fields over budget fail instead of forwarding oversized requests", async () => {
  await expect(
    compressBody({
      instructions: "x".repeat(4000),
      input: [{ role: "user", content: "small" }],
    }),
  ).rejects.toThrow(
    "protected turns or fixed request fields exceed token budget",
  );
});
test("cache write failures return 503 without retrying successful inference", async () => {
  const body = longBody();
  body.messages[1].content = "database-outage-unique " + "D".repeat(1300);
  const before = calls;
  db.exec(
    "CREATE TRIGGER reject_cache_write BEFORE INSERT ON compressed BEGIN SELECT RAISE(ABORT,'simulated cache failure'); END",
  );
  try {
    const result = await fetch(`${base}/compress`, {
      method: "POST",
      body: json(body),
    });
    expect(result.status).toBe(503);
    expect(calls - before).toBe(1);
  } finally {
    db.exec("DROP TRIGGER reject_cache_write");
  }
});

test("proxy passes original bytes and credentials, including gzip and zstd", async () => {
  const raw = Buffer.from(
    '{ "model": "test", "messages": [{"role":"user","content":"small"}] }',
  );
  for (const [encoding, body] of [
    ["identity", raw],
    ["gzip", gzipSync(raw)],
    ["zstd", zstdCompressSync(raw)],
  ] as const) {
    const result = await fetch(`${base}/r/test/echo?key=value`, {
      method: "POST",
      headers: { "Content-Encoding": encoding, Authorization: "Bearer caller" },
      body,
    });
    expect(result.status).toBe(200);
    expect(result.headers.get("X-Context-Router")).toBe("pass");
    expect(result.headers.get("X-Upstream-Auth")).toBe("Bearer caller");
    expect(Buffer.from(await result.arrayBuffer())).toEqual(Buffer.from(body));
  }
  expect((await fetch(`${base}/r/unknown/models`)).status).toBe(404);
  expect((await fetch(`${base}/r/test/encoded%2Fsegment?key=%2F`)).status).toBe(
    200,
  );
  expect(new URL(recordedUrl).pathname).toBe("/encoded%2Fsegment");
  expect(new URL(recordedUrl).search).toBe("?key=%2F");
});
test("proxy streams the first chunk without waiting for the end", async () => {
  const result = await fetch(`${base}/r/test/stream`),
    reader = result.body!.getReader();
  expect(new TextDecoder().decode((await reader.read()).value)).toBe(
    "data: first\n\n",
  );
  streamController!.enqueue(Buffer.from("data: last\n\n"));
  streamController!.close();
  expect(new TextDecoder().decode((await reader.read()).value)).toBe(
    "data: last\n\n",
  );
  expect((await reader.read()).done).toBe(true);
});
test("explicit Elysia routes reject unrelated paths and methods", async () => {
  expect(app.routes.map(({ method, path }) => `${method} ${path}`)).toEqual([
    "GET /health",
    "GET /stats",
    "POST /compress",
    "GET /api/settings",
    "PUT /api/settings",
    "GET /api/history",
    "GET /api/history/:id",
    "GET /dashboard",
    "GET /dashboard/assets/:file",
    "GET /r/:provider/*",
    "POST /r/:provider/*",
    "GET /codex/responses",
    "POST /codex/responses",
  ]);
  const before = calls;
  for (const [method, path] of [
    ["GET", "/unknown"],
    ["POST", "/health"],
    ["GET", "/compress"],
    ["DELETE", "/r/test/echo"],
    ["POST", "/r/unknown/echo"],
  ]) {
    expect((await fetch(`${base}${path}`, { method })).status).toBe(404);
  }
  expect(calls).toBe(before);
  for (const method of ["GET", "POST"])
    expect(
      (
        await fetch(`${base}/codex/responses`, {
          method,
          ...(method === "POST"
            ? { body: json({ messages: [{ role: "user", content: "small" }] }) }
            : {}),
        })
      ).status,
    ).toBe(200);
});
test("HTTP health, validation, compression and native cache schema", async () => {
  expect(await (await fetch(`${base}/health`)).text()).toBe('{"status":"ok"}');
  const post = (body: string) =>
    fetch(`${base}/compress`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
  expect((await post("{bad")).status).toBe(400);
  expect((await post("null")).status).toBe(400);
  expect((await post('{"messages":[null]}')).status).toBe(400);
  expect((await post(json(longBody()))).status).toBe(200);
  expect(
    (
      await fetch(`${base}/r/test/echo`, {
        method: "POST",
        headers: { "Content-Encoding": "gzip" },
        body: "not gzip",
      })
    ).status,
  ).toBe(400);
  expect(
    (db.query("PRAGMA table_info(compressed)").all() as any[])
      .filter((col) => col.pk)
      .sort((a, b) => a.pk - b.pk)
      .map((col) => col.name),
  ).toEqual(["key"]);
  const legacy = new Database(":memory:");
  legacy.exec(
    'CREATE TABLE summaries(digest TEXT,summary TEXT); INSERT INTO summaries VALUES("old","keep")',
  );
  initCache(legacy);
  initCache(legacy);
  expect(legacy.query("SELECT summary FROM summaries").get()).toEqual({
    summary: "keep",
  });
  expect(legacy.query("SELECT count(*) n FROM compressed").get()).toEqual({
    n: 0,
  });
  legacy.close();
  const oldCompressed = new Database(":memory:");
  oldCompressed.exec(
    'CREATE TABLE compressed (source_hash TEXT NOT NULL, kind TEXT NOT NULL, settings_hash TEXT NOT NULL, output TEXT NOT NULL, output_hash TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(source_hash,kind,settings_hash)); INSERT INTO compressed VALUES("src","block","set","out","outhash",12345); CREATE INDEX compressed_output ON compressed(output_hash);',
  );
  initCache(oldCompressed);
  initCache(oldCompressed);
  expect(
    (oldCompressed.query("PRAGMA index_list(compressed)").all() as any[]).some(
      (index) => index.name === "compressed_output_key",
    ),
  ).toBe(true);
  expect(
    oldCompressed.query("SELECT count(*) n FROM compressed").get(),
  ).toEqual({ n: 1 });
  expect(
    (oldCompressed.query("PRAGMA table_info(compressed)").all() as any[])
      .filter((col) => col.pk)
      .map((col) => col.name),
  ).toEqual(["key"]);
  expect(
    oldCompressed.query("SELECT key, output FROM compressed").get(),
  ).toEqual({
    key: "src-set",
    output: "out",
  });
  oldCompressed.close();
});
