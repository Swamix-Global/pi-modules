import { homedir } from "node:os";
const env = (key: string) => {
  const value = process.env[`PI_CONTEXT_ROUTER_${key}`];
  if (value === undefined)
    throw new Error(`Missing .env setting: PI_CONTEXT_ROUTER_${key}`);
  return value;
};
const number = (key: string, min: number) => {
  const value = Number(env(key));
  if (!env(key).trim() || !Number.isFinite(value) || value < min)
    throw new Error(`Invalid .env setting: ${key}`);
  return value;
};
export const S = {
  host: env("HOST"),
  port: number("PORT", 0),
  upstream: env("UPSTREAM"),
  routes: JSON.parse(env("ROUTES")) as Record<string, string>,
  db: env("DB").replace(/^~(?=\/|$)/, homedir()),
  auth: env("AUTH").replace(/^~(?=\/|$)/, homedir()),
  apiKey: env("API_KEY"),
  tokens: number("TOKENS", 1),
  keepTurns: number("KEEP_TURNS", 1),
  url: env("CONDENSER_URL"),
  model: env("CONDENSER_MODEL") as "gpt-6-luna" | "deepseek-flash",
  words: number("SUMMARY_WORDS", 1),
  maxTokens: number("MAX_TOKENS", 1),
  maxBody: number("MAX_BODY_BYTES", 1),
  blockPrompt: env("BLOCK_PROMPT"),
};
if (!["127.0.0.1", "::1", "localhost"].includes(S.host))
  throw new Error("ctx-engine must bind loopback");
for (const key of [
  "port",
  "keepTurns",
  "tokens",
  "maxBody",
  "maxTokens",
] as const)
  if (!Number.isInteger(S[key]))
    throw new Error(`Invalid integer setting: ${key}`);
if (!["gpt-6-luna", "deepseek-flash"].includes(S.model))
  throw new Error("Invalid condenser model");
if (S.port > 65535) throw new Error("Invalid port");
for (const endpoint of [S.url, S.upstream, ...Object.values(S.routes)])
  if (!["http:", "https:"].includes(new URL(endpoint).protocol))
    throw new Error("Invalid upstream URL");
