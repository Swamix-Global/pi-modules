import { Elysia } from "elysia";
import { Database } from "bun:sqlite";
import { mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { request as http, type IncomingMessage } from "node:http";
import { request as https } from "node:https";
import { Readable } from "node:stream";
import {
  gzipSync,
  gunzipSync,
  zstdCompressSync,
  zstdDecompressSync,
} from "node:zlib";
import { S } from "./settings.ts";
import { json, hash, tokens, turns, transcript, type Item } from "./utils.ts";

import { condense, ATTEMPT_TIMEOUT_MS, MAX_ATTEMPTS } from "./condenser.ts";
import { CacheWarmer, warmPlan, warmUsage, type WarmPlan } from "./warmer.ts";

export const warmer = new CacheWarmer(async (plan: WarmPlan, signal) => {
  const request = new Request(plan.url, {
    method: "POST",
    headers: plan.headers,
    signal,
  });
  const upstream = await send(
    new URL(plan.url),
    request,
    Buffer.from(plan.wire),
    false,
  );
  if (upstream.statusCode !== 200) {
    upstream.destroy();
    throw new Error("cache warm upstream failure");
  }
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const chunk of upstream) {
    bytes += chunk.length;
    if (bytes > 1024 * 1024) {
      upstream.destroy();
      throw new Error("cache warm response too large");
    }
    chunks.push(Buffer.from(chunk));
  }
  return warmUsage(Buffer.concat(chunks).toString("utf8"));
});

type Config = typeof S;
if (S.db !== ":memory:") mkdirSync(dirname(S.db), { recursive: true });
export const db = new Database(S.db, { create: true });
export function initCache(database: Database) {
  database.exec(`PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;`);
  const cols = database
    .query<{ name: string; pk: number }, []>("PRAGMA table_info(compressed)")
    .all();
  const pks = cols
    .filter((c) => c.pk > 0)
    .sort((a, b) => a.pk - b.pk)
    .map((c) => c.name);
  if (cols.length === 0) {
    database.exec(`
      CREATE TABLE IF NOT EXISTS compressed (
        key TEXT NOT NULL PRIMARY KEY,
        output TEXT NOT NULL,
        output_hash TEXT NOT NULL,
        created_at REAL NOT NULL
      );
    `);
  } else if (pks.length !== 1 || pks[0] !== "key") {
    // Rebuild copies, never drops (per AGENTS.md)
    const legacyName = `compressed_legacy_${Date.now()}`;
    database.transaction(() =>
      database.exec(`
      ALTER TABLE compressed RENAME TO ${legacyName};
      CREATE TABLE compressed (
        key TEXT NOT NULL PRIMARY KEY,
        output TEXT NOT NULL,
        output_hash TEXT NOT NULL,
        created_at REAL NOT NULL
      );
      INSERT OR IGNORE INTO compressed(key, output, output_hash, created_at)
        SELECT source_hash || '-' || settings_hash, output, output_hash, created_at FROM ${legacyName};
    `),
    )();
  }
  database.exec(
    "CREATE INDEX IF NOT EXISTS compressed_output_key ON compressed(output_hash)",
  );
}
initCache(db);
db.exec(`
  CREATE TABLE IF NOT EXISTS dashboard_settings (
    id INTEGER PRIMARY KEY CHECK(id = 1), value TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL, route TEXT NOT NULL, model TEXT NOT NULL,
    status INTEGER NOT NULL, changed INTEGER NOT NULL,
    before_tokens INTEGER NOT NULL, after_tokens INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL, error TEXT,
    original TEXT, compressed TEXT, changes TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS history_created ON history(created_at);
`);
db.query(
  "UPDATE history SET status=502,error='router stopped before completion',duration_ms=max(0,?-created_at) WHERE status=0",
).run(Date.now());
const cached = db.query<{ output: string }, [string]>(
  "SELECT output FROM compressed WHERE key=?",
);
const rendered = db.query(
  "SELECT 1 FROM compressed WHERE output_hash=? LIMIT 1",
);
const save = db.query(
  "INSERT OR IGNORE INTO compressed(key, output, output_hash, created_at) VALUES (?,?,?,?)",
);
const stats = {
  requests: 0,
  blocks: 0,
  hits: 0,
  errors: 0,
};
class HttpError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
const settingLimits = {
  tokens: [1, 10_000_000],
  keepTurns: [1, 1000],
  words: [1, 100_000],
  maxTokens: [1, 1_000_000],
} as const;
const settingKeys = [
  ...Object.keys(settingLimits),
  "blockPrompt",
  "model",
] as (keyof Config)[];
const defaults = Object.fromEntries(settingKeys.map((key) => [key, S[key]]));
const currentSettings = () =>
  Object.fromEntries(settingKeys.map((key) => [key, S[key]]));
function validateSettings(value: unknown): Record<string, number | string> {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new HttpError(400, "settings must be an object");
  for (const [key, item] of Object.entries(value)) {
    if (!settingKeys.includes(key as keyof Config))
      throw new HttpError(400, `unknown setting: ${key}`);
    if (key === "blockPrompt") {
      if (typeof item !== "string" || !item.trim() || item.length > 32_768)
        throw new HttpError(400, `invalid setting: ${key}`);
    } else if (key === "model") {
      if (
        typeof item !== "string" ||
        !["gpt-6-luna", "deepseek-flash"].includes(item)
      )
        throw new HttpError(400, `invalid setting: ${key}`);
    } else {
      const [min, max] = settingLimits[key as keyof typeof settingLimits];
      if (
        typeof item !== "number" ||
        !Number.isFinite(item) ||
        item < min ||
        item > max ||
        !Number.isSafeInteger(item)
      )
        throw new HttpError(400, `invalid setting: ${key}`);
    }
  }
  return value as Record<string, number | string>;
}
const savedSettings = db
  .query<{ value: string }, []>(
    "SELECT value FROM dashboard_settings WHERE id=1",
  )
  .get();
if (savedSettings) {
  // Remove retired controls from persisted settings before validating the current schema.
  const value = JSON.parse(savedSettings.value);
  const retained = Object.fromEntries(
    Object.entries(value).filter(([key]) =>
      settingKeys.includes(key as keyof Config),
    ),
  );
  Object.assign(S, validateSettings(retained));
  db.query("UPDATE dashboard_settings SET value=? WHERE id=1").run(
    json(currentSettings()),
  );
}

function managementRequest(request: Request) {
  const url = new URL(request.url);
  if (!["127.0.0.1", "localhost", "[::1]"].includes(url.hostname))
    throw new HttpError(403, "local dashboard only");
  const origin = request.headers.get("origin");
  if (
    (origin && origin !== url.origin) ||
    request.headers.get("sec-fetch-site") === "cross-site"
  )
    throw new HttpError(403, "cross-origin dashboard request denied");
}
function errorStatus(error: unknown) {
  if (error instanceof HttpError) return error.status;
  return error instanceof Error &&
    (error.name === "SQLiteError" ||
      (error as NodeJS.ErrnoException).code?.startsWith("SQLITE"))
    ? 503
    : 502;
}
type Trace = {
  id: number | bigint;
  original: any;
  compressed: any;
  changed: boolean;
  changes: Item[];
};
function rememberOriginal(trace: Trace) {
  db.query(
    "UPDATE history SET original=?,model=?,before_tokens=? WHERE id=?",
  ).run(
    json(trace.original),
    typeof trace.original?.model === "string" ? trace.original.model : "",
    tokens(trace.original),
    trace.id,
  );
}
async function tracked(
  request: Request,
  action: (trace: Trace) => Promise<Response>,
) {
  const start = Date.now();
  const trace: Trace = {
    id: 0,
    original: null,
    compressed: null,
    changed: false,
    changes: [],
  };
  const inserted = db
    .query(
      "INSERT INTO history(created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error,original,compressed,changes) VALUES(?,?,?,0,0,0,0,0,NULL,NULL,NULL,'[]')",
    )
    .run(start, `${request.method} ${new URL(request.url).pathname}`, "");
  trace.id = inserted.lastInsertRowid;
  stats.requests++;
  let status = 0,
    error: string | null = null;
  try {
    const result = await action(trace);
    status = result.status;
    return result;
  } catch (cause) {
    status = errorStatus(cause);
    error =
      cause instanceof HttpError
        ? cause.message
        : status === 503
          ? "cache database unavailable"
          : "context router upstream failure";
    throw cause;
  } finally {
    db.query(
      "UPDATE history SET model=?,status=?,changed=?,before_tokens=?,after_tokens=?,duration_ms=?,error=?,original=?,compressed=?,changes=? WHERE id=?",
    ).run(
      typeof trace.original?.model === "string" ? trace.original.model : "",
      status,
      Number(trace.changed),
      trace.original === null ? 0 : tokens(trace.original),
      trace.compressed === null ? 0 : tokens(trace.compressed),
      Date.now() - start,
      error,
      trace.original === null ? null : json(trace.original),
      trace.compressed === null ? null : json(trace.compressed),
      json(trace.changes),
      inserted.lastInsertRowid,
    );
  }
}
function listHistory(request: Request) {
  managementRequest(request);
  const query = new URL(request.url).searchParams;
  const limit = Number(query.get("limit") ?? 50),
    before = query.has("before") ? Number(query.get("before")) : null;
  const changed = query.get("changed") ?? "all",
    search = query.get("search") ?? "";
  if (
    !Number.isSafeInteger(limit) ||
    limit < 1 ||
    limit > 100 ||
    (before !== null && (!Number.isSafeInteger(before) || before < 1)) ||
    !["all", "yes", "no"].includes(changed) ||
    search.length > 200
  )
    throw new HttpError(400, "invalid history query");
  const where =
    "(?='all' OR changed=?) AND (instr(lower(route),lower(?))>0 OR instr(lower(model),lower(?))>0)";
  const filter = [changed, Number(changed === "yes"), search, search] as const;
  const items = db
    .query(
      `SELECT id,created_at,route,model,status,changed,before_tokens,after_tokens,duration_ms,error FROM history WHERE ${where} AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?`,
    )
    .all(...filter, before, before, limit + 1) as Item[];
  const total = (
    db
      .query(`SELECT count(*) n FROM history WHERE ${where}`)
      .get(...filter) as { n: number }
  ).n;
  const next = items.length > limit ? items[limit - 1].id : null;
  return response({ items: items.slice(0, limit), total, next });
}
const response = (
  value: any,
  status = 200,
  headers: Record<string, string> = {},
) =>
  new Response(json(value), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });

async function summarize(
  source: any,
  input: string,
  outputHash: (output: string) => string,
  config: Config,
): Promise<string | null> {
  const S = config;
  const key = `${hash(source)}-${S.model}-${S.maxTokens}`;
  const hit = cached.get(key);
  if (hit) {
    stats.hits++;
    return hit.output;
  }
  for (let attempt = 1; attempt <= MAX_ATTEMPTS; attempt++) {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let output: string;
    try {
      // Bound the entire attempt, including OAuth and response-body reads.
      const deadline = new Promise<never>((_, reject) => {
        timer = setTimeout(() => {
          controller.abort();
          reject(new Error("Condenser timeout"));
        }, ATTEMPT_TIMEOUT_MS);
      });
      output = await Promise.race([
        condense(input, config, controller.signal),
        deadline,
      ]);
    } catch {
      if (attempt === MAX_ATTEMPTS) {
        stats.errors++;
        // Never log credentials, request text or provider response bodies.
        console.error(
          `CRITICAL: condenser exhausted ${MAX_ATTEMPTS} attempts (${ATTEMPT_TIMEOUT_MS}ms each); model=${S.model} message=${hash(source)}`,
        );
      }
      continue;
    } finally {
      clearTimeout(timer);
    }
    save.run(key, output, outputHash(output), Date.now() / 1000);
    stats.blocks++;
    return output;
  }
  return null;
}
export async function compressBody(
  original: Item,
  changes: Item[] = [],
  config: Config = { ...S },
): Promise<{ body: Item; changed: boolean }> {
  const S = config;
  const key = Array.isArray(original.messages)
    ? "messages"
    : Array.isArray(original.input)
      ? "input"
      : null;
  if (!key) return { body: original, changed: false };
  if (
    original[key].some(
      (item: any) => !item || typeof item !== "object" || Array.isArray(item),
    )
  )
    throw new HttpError(400, "messages must contain objects");
  const body = structuredClone(original),
    [prefix, groups] = turns(body[key]);
  const summaryItem = (output: string) => ({
    role: "user",
    content:
      key === "messages" ? output : [{ type: "input_text", text: output }],
  });
  let changed = false;
  // ponytail: unbounded fanout; downstream waits for every settled-turn block.
  if (tokens(body) > S.tokens) {
    const completed = Math.max(0, groups.length - S.keepTurns);
    await Promise.all(
      groups.slice(0, completed).map(async (block, index) => {
        if (rendered.get(hash(block))) {
          stats.hits++;
          return;
        }
        const key = `${hash(block)}-${config.model}-${config.maxTokens}`;
        const wasCached = !!cached.get(key);
        const output = await summarize(
          block,
          transcript(block),
          (output) => hash([summaryItem(output)]),
          config,
        );
        if (output) {
          groups[index] = [summaryItem(output)];
          changes.push({
            kind: "block",
            source_hash: hash(block),
            before: structuredClone(block),
            after: groups[index],
            cached: wasCached,
            fallback: false,
          });
          changed = true;
        }
      }),
    );
  }
  body[key] = [...prefix, ...groups.flat()];
  while (tokens(body) > S.tokens && groups.length > S.keepTurns) {
    const removed = groups.shift()!;
    changes.push({
      kind: "drop",
      source_hash: hash(removed),
      before: structuredClone(removed),
      after: null,
      cached: false,
      fallback: true,
    });
    body[key] = [...prefix, ...groups.flat()];
    changed = true;
  }
  const remaining = tokens(body);
  if (remaining > S.tokens)
    // The configured budget is a soft compaction threshold, not the model's
    // context window: forward indivisible protected turns and fixed fields
    // instead of rejecting the request. The upstream enforces its real limit.
    console.warn(
      `WARN: protected turns or fixed fields exceed the ${S.tokens}-token budget (${remaining}); forwarding unchanged`,
    );
  return { body: changed ? body : original, changed };
}
const hop = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);
async function readBody(request: Request) {
  const declared = request.headers.get("content-length");
  if (declared !== null && !/^\d+$/.test(declared))
    throw new HttpError(400, "invalid content length");
  if (Number(declared) > S.maxBody)
    throw new HttpError(413, "request body too large");
  const reader = request.body?.getReader(),
    parts: Uint8Array[] = [];
  let size = 0;
  if (reader)
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.length;
        if (size > S.maxBody) {
          await reader.cancel();
          throw new HttpError(413, "request body too large");
        }
        parts.push(value);
      }
    } finally {
      reader.releaseLock();
    }
  if (declared !== null && Number(declared) !== size)
    throw new HttpError(400, "invalid request body size");
  return Buffer.concat(parts);
}
function send(
  url: URL,
  request: Request,
  body?: Uint8Array,
  retry = true,
): Promise<IncomingMessage> {
  const excluded = new Set([
    ...hop,
    "host",
    "content-length",
    "accept-encoding",
    ...(request.headers
      .get("connection")
      ?.toLowerCase()
      .split(",")
      .map((s) => s.trim()) ?? []),
  ]);
  const headers = Object.fromEntries(
    [...request.headers].filter(([key]) => !excluded.has(key.toLowerCase())),
  );
  if (body) headers["content-length"] = String(body.length);
  return new Promise((resolve, reject) => {
    const outgoing = (url.protocol === "https:" ? https : http)(
      url,
      { method: request.method, headers },
      resolve,
    );
    const abort = () => outgoing.destroy(new Error("client disconnected"));
    request.signal.addEventListener("abort", abort, { once: true });
    outgoing.once("close", () =>
      request.signal.removeEventListener("abort", abort),
    );
    outgoing.setTimeout(600_000, () =>
      outgoing.destroy(new Error("upstream timeout")),
    );
    outgoing.on("error", (error: NodeJS.ErrnoException) =>
      retry && ["EAI_AGAIN", "ENOTFOUND"].includes(error.code!)
        ? send(url, request, body, false).then(resolve, reject)
        : reject(error),
    );
    outgoing.end(body);
  });
}
function proxy(request: Request, upstream: string, rest: string) {
  return tracked(request, (trace) =>
    forwardProxy(request, upstream, rest, trace),
  );
}
async function forwardProxy(
  request: Request,
  upstream: string,
  rest: string,
  trace: Trace,
) {
  const receivedAt = Date.now();
  const target = new URL(upstream);
  target.pathname = target.pathname.replace(/\/+$/, "") + rest;
  target.search = new URL(request.url).search;
  let body: Uint8Array | undefined,
    changed = false;
  if (request.method === "POST") {
    const encoding =
      request.headers.get("content-encoding")?.toLowerCase() || "identity";
    if (!["identity", "gzip", "zstd"].includes(encoding))
      throw new HttpError(415, "unsupported content encoding");
    const raw = await readBody(request);
    let decoded: Uint8Array;
    try {
      decoded =
        encoding === "gzip"
          ? gunzipSync(raw, { maxOutputLength: S.maxBody })
          : encoding === "zstd"
            ? zstdDecompressSync(raw, { maxOutputLength: S.maxBody })
            : raw;
    } catch {
      throw new HttpError(400, "invalid compressed request body");
    }
    if (decoded.length > S.maxBody)
      throw new HttpError(413, "request body too large");
    let value: Item | null = null;
    try {
      value = JSON.parse(Buffer.from(decoded).toString("utf8"));
    } catch {}
    trace.original = value ?? Buffer.from(decoded).toString("utf8");
    rememberOriginal(trace);
    body = raw;
    trace.compressed = trace.original;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      trace.compressed = null;
      const credential = request.headers
        .get("authorization")
        ?.match(/^Bearer\s+(.+)$/i)?.[1];
      const removed: Item = {};
      let compatible = value;
      if (
        new URL(request.url).pathname === "/r/openai/responses" &&
        credential &&
        !credential.startsWith("sk-")
      ) {
        // Match Pi's direct-OpenAI subscription normalization, lost with a loopback base URL.
        compatible = { ...value };
        for (const key of [
          "max_output_tokens",
          "temperature",
          "prompt_cache_retention",
          "prompt_cache_options",
        ]) {
          if (Object.hasOwn(compatible, key)) {
            removed[key] = compatible[key];
            delete compatible[key];
          }
        }
      }
      const normalized = Object.keys(removed).length > 0;
      if (normalized)
        trace.changes.push({
          kind: "subscription",
          source_hash: hash(removed),
          before: removed,
          after: null,
          cached: false,
          fallback: false,
        });
      const result = await compressBody(compatible, trace.changes);
      trace.compressed = result.body;
      trace.changed = result.changed || normalized;
      changed = trace.changed;
      if (changed) {
        const wire = Buffer.from(json(result.body));
        body =
          encoding === "gzip"
            ? gzipSync(wire)
            : encoding === "zstd"
              ? zstdCompressSync(wire)
              : wire;
      }
    }
  }
  let finishWarm: ((success: boolean) => void) | undefined;
  if (trace.compressed && typeof trace.compressed === "object") {
    const plan = warmPlan(
      target,
      request,
      trace.compressed,
      (digest) => !!rendered.get(digest),
    );
    if (plan) finishWarm = warmer.watch(plan, receivedAt);
  }
  let upstreamResponse: IncomingMessage;
  try {
    upstreamResponse = await send(target, request, body);
  } catch (error) {
    finishWarm?.(false);
    throw error;
  }
  if (finishWarm) {
    const finish = finishWarm;
    upstreamResponse.once("end", () =>
      finish(upstreamResponse.statusCode === 200),
    );
    upstreamResponse.once("error", () => finish(false));
    upstreamResponse.once("close", () => {
      if (!upstreamResponse.complete) finish(false);
    });
  }
  const headers = new Headers();
  const excluded = new Set([
    ...hop,
    ...String(upstreamResponse.headers.connection ?? "")
      .toLowerCase()
      .split(",")
      .map((s) => s.trim()),
  ]);
  for (let i = 0; i < upstreamResponse.rawHeaders.length; i += 2)
    if (!excluded.has(upstreamResponse.rawHeaders[i].toLowerCase()))
      headers.append(
        upstreamResponse.rawHeaders[i],
        upstreamResponse.rawHeaders[i + 1],
      );
  headers.set("X-Context-Router", changed ? "compacted" : "pass");
  const status = upstreamResponse.statusCode!;
  if ([204, 304].includes(status)) {
    upstreamResponse.resume();
    return new Response(null, { status, headers });
  }
  return new Response(
    Readable.toWeb(upstreamResponse) as unknown as ReadableStream<Uint8Array>,
    { status, headers },
  );
}
function providerProxy({
  request,
  params,
}: {
  request: Request;
  params: { provider: string; "*": string };
}) {
  if (!Object.hasOwn(S.routes, params.provider))
    throw new HttpError(404, "not found");
  const pathname = new URL(request.url).pathname;
  return proxy(
    request,
    S.routes[params.provider],
    pathname.slice(pathname.indexOf("/", 3)),
  );
}
const codexProxy = ({ request }: { request: Request }) =>
  proxy(request, S.upstream, "/codex/responses");
const timelineQuery = db.query<
  {
    time: number;
    requests: number;
    compacted: number;
    errors: number;
    before_tokens: number;
    after_tokens: number;
  },
  [number]
>(`
  SELECT
    ((created_at / 300000) * 300000) AS time,
    count(*) AS requests,
    coalesce(sum(changed), 0) AS compacted,
    coalesce(sum(status >= 400), 0) AS errors,
    coalesce(sum(before_tokens), 0) AS before_tokens,
    coalesce(sum(after_tokens), 0) AS after_tokens
  FROM history
  WHERE created_at >= ?
  GROUP BY time
  ORDER BY time ASC
`);

export const app = new Elysia()
  .onError(({ error, code }) => {
    if (error instanceof HttpError)
      return response({ error: error.message }, error.status);
    if (code === "NOT_FOUND") return response({ error: "not found" }, 404);
    if (code === "PARSE" || code === "VALIDATION")
      return response({ error: "invalid request" }, 400);
    if (
      error instanceof Error &&
      (error.name === "SQLiteError" ||
        (error as NodeJS.ErrnoException).code?.startsWith("SQLITE"))
    )
      return response({ error: "cache database unavailable" }, 503);
    return response({ error: "context router upstream failure" }, 502);
  })
  .onBeforeHandle(({ request }) => {
    const path = new URL(request.url).pathname;
    if (
      path.startsWith("/api/") ||
      path === "/stats" ||
      path === "/dashboard" ||
      path.startsWith("/dashboard/")
    )
      managementRequest(request);
  })
  .get("/health", () => response({ status: "ok" }))
  .get("/stats", () => {
    const now = Date.now();
    return response({
      now,
      ...stats,
      warmer: warmer.stats,
      history: db
        .query(
          `SELECT count(*) requests, coalesce(sum(changed),0) compacted,
        coalesce(sum(status>=400),0) errors, coalesce(sum(before_tokens),0) before_tokens,
        coalesce(sum(after_tokens),0) after_tokens, coalesce(sum(duration_ms),0) duration_ms FROM history`,
        )
        .get(),
      cache: db.query("SELECT count(*) AS n FROM compressed").get(),
      timeline: timelineQuery.all(now - 86_400_000),
    });
  })
  .post(
    "/compress",
    ({ request }) =>
      tracked(request, async (trace) => {
        let value: Item;
        try {
          const raw = (await readBody(request)).toString("utf8");
          trace.original = raw;
          value = JSON.parse(raw);
          trace.original = value;
          rememberOriginal(trace);
        } catch (error) {
          if (error instanceof HttpError) throw error;
          throw new HttpError(400, "invalid JSON");
        }
        if (!value || typeof value !== "object" || Array.isArray(value))
          throw new HttpError(400, "invalid JSON");
        const result = await compressBody(value, trace.changes);
        trace.compressed = result.body;
        trace.changed = result.changed;
        return response(result.body, 200, {
          "X-Context-Router": result.changed ? "compacted" : "pass",
        });
      }),
    { parse: "none" },
  )
  .get("/api/settings", () =>
    response({ settings: currentSettings(), defaults }),
  )
  .put(
    "/api/settings",
    async ({ request }) => {
      let value: unknown;
      try {
        value = JSON.parse((await readBody(request)).toString("utf8"));
      } catch (error) {
        if (error instanceof HttpError) throw error;
        throw new HttpError(400, "invalid JSON");
      }
      const updated = { ...currentSettings(), ...validateSettings(value) };
      db.query(
        "INSERT INTO dashboard_settings(id,value) VALUES(1,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
      ).run(json(updated));
      Object.assign(S, updated);
      return response({ settings: currentSettings(), defaults });
    },
    { parse: "none" },
  )
  .get("/api/history", ({ request }) => listHistory(request))
  .get("/api/history/:id", ({ params }) => {
    const id = Number(params.id);
    if (!Number.isSafeInteger(id) || id < 1)
      throw new HttpError(400, "invalid history id");
    const row = db
      .query("SELECT * FROM history WHERE id=?")
      .get(id) as Item | null;
    if (!row) throw new HttpError(404, "history not found");
    return response({
      ...row,
      original: row.original === null ? null : JSON.parse(row.original),
      compressed: row.compressed === null ? null : JSON.parse(row.compressed),
      changes: JSON.parse(row.changes),
    });
  })
  .get(
    "/dashboard",
    () =>
      new Response(
        Bun.file(join(import.meta.dir, "frontend/dist/index.html")),
        { headers: { "Cache-Control": "no-cache" } },
      ),
  )
  .get("/dashboard/assets/:file", async ({ params }) => {
    if (!/^[A-Za-z0-9_-]+\.(js|css|woff2|svg)$/.test(params.file))
      throw new HttpError(404, "not found");
    const file = Bun.file(
      join(import.meta.dir, "frontend/dist/assets", params.file),
    );
    if (!(await file.exists())) throw new HttpError(404, "not found");
    return new Response(file, {
      headers: { "Cache-Control": "public, max-age=31536000, immutable" },
    });
  })
  .get("/r/:provider/*", providerProxy)
  .post("/r/:provider/*", providerProxy, { parse: "none" })
  .get("/codex/responses", codexProxy)
  .post("/codex/responses", codexProxy, { parse: "none" });
if (import.meta.main) {
  app.listen({
    hostname: S.host,
    port: S.port,
    maxRequestBodySize: S.maxBody,
    idleTimeout: 0,
  });
  console.log(`ctx-engine listening on ${S.host}:${app.server!.port}`);
  const stop = async () => {
    warmer.stop();
    await app.stop(true);
    db.close();
    process.exit(0);
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}
