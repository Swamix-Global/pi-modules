// Typed access to the ctx-engine router API. Every value the UI renders comes through here as
// plain JSON and is rendered as React text — never injected as HTML.

export interface Cost {
  model?: string;
  estimated?: boolean;
  in?: number;
  cached?: number;
  out?: number;
  raw_cached?: number;
  raw_usd?: number;
  sent_usd?: number;
  condenser_usd?: number;
  condenser_calls?: number;
  condenser_in?: number;
  condenser_out?: number;
  net_usd?: number;
  net_percent?: number;
}

export interface RecentRequest {
  id: number;
  at: number;
  route: string;
  model: string;
  result: string;
  status: number | null;
  ms: number | null;
  raw_tokens: number;
  sent_tokens: number;
  provider: string;
  detail: string;
  raw: string;
  cost: Cost | null;
}

export interface SeriesPoint {
  minute: number; // epoch minutes
  raw_tokens: number;
  sent_tokens: number;
  saved_tokens: number;
  count: number;
  net_usd: number;
}

export interface WindowStats {
  size: number;
  span_s?: number;
  requests_per_min?: number;
  results?: Record<string, number>;
  pass_rate?: number;
  compact_rate?: number;
  error_rate?: number;
  avg_ms?: number;
  p50_ms?: number;
  p95_ms?: number;
  max_ms?: number;
  raw_tokens?: number;
  sent_tokens?: number;
  saved_tokens?: number;
  saved_percent?: number;
  raw_usd?: number;
  upstream_usd?: number;
  condenser_usd?: number;
  net_usd?: number;
  net_percent?: number;
  routes?: Record<string, number>;
  models?: Record<string, number>;
  providers?: Record<string, number>;
}

export interface GlobalStats {
  size: number;
  raw_tokens: number;
  sent_tokens: number;
  saved_tokens: number;
  saved_percent: number;
  raw_usd: number;
  upstream_usd: number;
  net_usd: number;
  net_percent: number;
  first_at: number | null;
  last_at: number | null;
}

export interface Stats {
  requests: number;
  passed: number;
  compacted: number;
  images: number;
  trimmed: number;
  overflow: number;
  blocks_seen: number;
  blocks_summarized: number;
  blocks_pending: number;
  blocks_dropped?: number;
  cache_hits: number;
  cache_entries: number;
  cache_buckets: Record<string, number>;
  condenser_calls_deepseek: number;
  condenser_calls: number;
  condenser_avg_ms: number;
  condenser_ms_total: number;
  condenser_tokens_in: number;
  condenser_tokens_out: number;
  condenser_cost_usd: number;
  condenser_errors: number;
  summary_tokens: number;
  summary_token_steps: number[];
  token_limit: number;
  provider: string;
  model: string;
  uptime_s: number;
  started: number;
  now: number;
  global: GlobalStats;
  window: WindowStats;
  series: SeriesPoint[];
  recent: RecentRequest[];
  last_compaction: number | null;
  chars_per_token: number;
  input_chars_raw: number;
  input_chars_sent: number;
  input_tokens_raw_est: number;
  input_tokens_sent_est: number;
  saved_percent: number;
  cache_db: string;
  db_errors: number;
  db_error_at: number | null;
}

export interface PlanBlock {
  id: string; // first 16 hex chars of the block SHA-256
  tokens?: number;
  summary_tokens?: number;
  chars?: number; // legacy rows in the request ledger
  summary_chars?: number; // legacy rows in the request ledger
  pending?: boolean;
  dropped?: boolean;
  preview?: string;
  summary?: string;
}

export interface CompactionPlan {
  blocks?: PlanBlock[];
  kept?: number;
  omitted?: number;
}

export interface RequestDetail {
  id: number;
  at: number;
  route: string;
  model: string;
  result: string;
  status: number | null;
  ms: number | null;
  raw_tokens: number;
  sent_tokens: number;
  provider: string;
  detail: string;
  plan: CompactionPlan | null;
  cost: Cost | null;
}

// Local model usage aggregate reported by the inference server, kept separate from the router
// token ledger. `source` names the local backend; these numbers never mix with router tokens.
export interface TokenTotals {
  requests: number;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  elapsed_ms: number;
}

export interface TokenBySource extends TokenTotals {
  source: string;
}

export interface TokenRecent {
  seq: number;
  source: string;
  model: string;
  status: string;
  input_tokens: number;
  output_tokens: number;
  reasoning_tokens: number;
  cached_tokens: number;
  elapsed_ms: number;
  started: number;
}

export interface TokenStats {
  now: number;
  totals: TokenTotals;
  by_source: TokenBySource[];
  recent: TokenRecent[];
}

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly path: string,
  ) {
    super(`${path} → HTTP ${status}`);
    this.name = "ApiError";
  }
}

export function isMissingEndpoint(error: unknown): boolean {
  return error instanceof ApiError && (error.status === 404 || error.status === 405 || error.status === 501);
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(path, { headers: { accept: "application/json" }, cache: "no-store" });
  if (!res.ok) throw new ApiError(res.status, path);
  return (await res.json()) as T;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new ApiError(res.status, path);
  return (await res.json()) as T;
}

export const api = {
  stats: () => getJson<Stats>("/stats"),
  tokenStats: () => getJson<TokenStats>("/token-stats"),
  request: (id: number) => getJson<RequestDetail>(`/request/${id}`),
  setConfig: (config: { tokens?: number; summary_tokens?: number }) => postJson<unknown>("/config", config),
  setProvider: (provider: string) => postJson<unknown>("/provider", { provider }),
};
