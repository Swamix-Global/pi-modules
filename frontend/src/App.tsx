import { useCallback, useEffect, useRef, useState } from "react";
import { diffLines } from "diff";

/* Types ------------------------------------------------------------------- */

type Tab = "overview" | "history" | "settings";
type ChangedFilter = "all" | "yes" | "no";

interface HistoryStats {
  requests: number;
  compacted: number;
  errors: number;
  before_tokens: number;
  after_tokens: number;
  duration_ms: number;
}

interface Stats {
  requests: number;
  blocks: number;
  hits: number;
  errors: number;
  cache: { n: number };
  history?: HistoryStats;
}

interface HistoryItem {
  id: number;
  created_at: number;
  route: string;
  model: string;
  status: number;
  changed: boolean;
  before_tokens: number;
  after_tokens: number;
  duration_ms: number;
  error: string | null;
}

interface HistoryResponse {
  items: HistoryItem[];
  total: number;
  next: number | null;
}

interface RouterChange {
  kind: string;
  source_hash: string;
  before: unknown;
  after: unknown;
  cached: boolean;
  fallback: boolean;
}

interface HistoryDetail {
  id: number;
  created_at: number;
  route: string;
  model: string;
  status: number;
  changed: boolean;
  before_tokens: number;
  after_tokens: number;
  duration_ms: number;
  error: string | null;
  original: unknown | null;
  compressed: unknown | null;
  changes: RouterChange[];
  [key: string]: unknown;
}

type ModelId = "gpt-6-luna" | "deepseek-flash";

interface Settings {
  tokens: number;
  keepTurns: number;
  words: number;
  maxTokens: number;
  blockPrompt: string;
  model: ModelId;
}

interface SettingsResponse {
  settings: Settings;
  defaults: Settings;
}

type NumericKey = Exclude<keyof Settings, "blockPrompt" | "model">;
type PromptKey = "blockPrompt";

interface NumericField {
  key: NumericKey;
  label: string;
  min: number;
  max?: number;
  step?: number;
  unit?: string;
  help: string;
}

interface DiffRow {
  oldNo: number | null;
  newNo: number | null;
  type: "add" | "del" | "ctx";
  text: string;
}

/* Constants --------------------------------------------------------------- */

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "history", label: "History" },
  { id: "settings", label: "Settings" },
];

const NUMERIC_FIELDS: NumericField[] = [
  {
    key: "tokens",
    label: "Context token budget",
    min: 1,
    step: 1,
    unit: "tokens",
    help: "Requests estimated above this budget are compressed.",
  },
  {
    key: "keepTurns",
    label: "Recent turns protected from block summaries",
    min: 1,
    step: 1,
    unit: "turns",
    help: "Newest turns are preserved; no clipping.",
  },
  {
    key: "words",
    label: "Summary target length",
    min: 1,
    step: 1,
    unit: "words",
    help: "Approximate word count requested from the summarizer.",
  },
  {
    key: "maxTokens",
    label: "Maximum summary tokens",
    min: 1,
    step: 1,
    unit: "tokens",
    help: "DeepSeek output safety headroom; OpenAI subscription follows the system word-length hint.",
  },
];

const MODEL_OPTIONS: { value: ModelId; label: string }[] = [
  { value: "gpt-6-luna", label: "Codex Luna 6 (existing Pi subscription)" },
  { value: "deepseek-flash", label: "DeepSeek Flash (low reasoning)" },
];

/* Helpers ----------------------------------------------------------------- */

async function readErrorDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { error?: string };
    return body && typeof body.error === "string" ? body.error : "";
  } catch {
    return "";
  }
}

async function getJson<T>(url: string): Promise<T> {
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(
      `${response.status} ${response.statusText}${detail ? `: ${detail}` : ""}`,
    );
  }
  return (await response.json()) as T;
}

async function putJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(
      `${response.status} ${response.statusText}${detail ? `: ${detail}` : ""}`,
    );
  }
  return (await response.json()) as T;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatNumber(value: number): string {
  return Number.isFinite(value) ? value.toLocaleString() : "—";
}

function formatTokens(value: number): string {
  return `~${formatNumber(value)}`;
}

function formatDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)} s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes}m ${remainder}s`;
}

function formatTime(epochMs: number): string {
  if (!Number.isFinite(epochMs)) return "—";
  return new Date(epochMs).toLocaleString();
}

function pretty(value: unknown): string {
  const text = JSON.stringify(value, null, 2);
  return text === undefined ? String(value) : text;
}

function mergeHistory(
  existing: HistoryItem[],
  incoming: HistoryItem[],
): HistoryItem[] {
  const byId = new Map<number, HistoryItem>();
  for (const item of existing) byId.set(item.id, item);
  for (const item of incoming) byId.set(item.id, item);
  return Array.from(byId.values()).sort((a, b) => b.id - a.id);
}

function toNumberStrings(settings: Settings): Record<NumericKey, string> {
  const out = {} as Record<NumericKey, string>;
  for (const field of NUMERIC_FIELDS)
    out[field.key] = String(settings[field.key]);
  return out;
}

function buildSettingsPayload(
  numbers: Record<NumericKey, string>,
  prompts: Record<PromptKey, string>,
  model: ModelId,
): { payload?: Record<string, number | string>; error?: string } {
  const payload: Record<string, number | string> = {};
  for (const field of NUMERIC_FIELDS) {
    const raw = numbers[field.key];
    const value = Number(raw);
    if (raw.trim() === "" || !Number.isFinite(value)) {
      return { error: `${field.label} must be a number.` };
    }
    if (value < field.min) {
      return { error: `${field.label} must be at least ${field.min}.` };
    }
    if (field.max !== undefined && value > field.max) {
      return { error: `${field.label} must be at most ${field.max}.` };
    }
    payload[field.key] = value;
  }
  payload.blockPrompt = prompts.blockPrompt;
  payload.model = model;
  return { payload };
}

/* App --------------------------------------------------------------------- */

export default function App() {
  const [tab, setTab] = useState<Tab>("overview");

  return (
    <div className="app">
      <header className="app-header">
        <h1 className="app-title">Context Router</h1>
        <p className="app-subtitle">
          Compression dashboard served from /dashboard/ using the same-origin
          API.
        </p>
        <nav
          className="tabs"
          role="tablist"
          aria-label="Dashboard sections"
          onKeyDown={(event) => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
              return;
            event.preventDefault();
            const index = TABS.findIndex((entry) => entry.id === tab);
            const next =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? TABS.length - 1
                  : (index +
                      (event.key === "ArrowRight" ? 1 : -1) +
                      TABS.length) %
                    TABS.length;
            setTab(TABS[next].id);
            document.getElementById(`tab-${TABS[next].id}`)?.focus();
          }}
        >
          {TABS.map((entry) => (
            <button
              key={entry.id}
              id={`tab-${entry.id}`}
              type="button"
              role="tab"
              aria-selected={tab === entry.id}
              aria-controls={`panel-${entry.id}`}
              tabIndex={tab === entry.id ? 0 : -1}
              className="tab"
              onClick={() => setTab(entry.id)}
            >
              {entry.label}
            </button>
          ))}
        </nav>
      </header>

      <main className="app-main">
        <section
          id="panel-overview"
          role="tabpanel"
          aria-labelledby="tab-overview"
          hidden={tab !== "overview"}
        >
          <Overview />
        </section>

        <section
          id="panel-history"
          role="tabpanel"
          aria-labelledby="tab-history"
          hidden={tab !== "history"}
        >
          <HistoryPanel />
        </section>

        <section
          id="panel-settings"
          role="tabpanel"
          aria-labelledby="tab-settings"
          hidden={tab !== "settings"}
        >
          <SettingsPanel />
        </section>
      </main>
    </div>
  );
}

/* Overview ---------------------------------------------------------------- */

function Metric({
  label,
  value,
  help,
}: {
  label: string;
  value: string;
  help: string;
}) {
  return (
    <div className="card">
      <p className="metric-label">{label}</p>
      <p className="metric-value">{value}</p>
      <p className="metric-help">{help}</p>
    </div>
  );
}

function Overview() {
  const [stats, setStats] = useState<Stats | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    const load = async () => {
      try {
        const data = await getJson<Stats>("/stats");
        if (active) {
          setStats(data);
          setError(null);
        }
      } catch (loadError) {
        if (active) setError(messageOf(loadError));
      }
    };

    void load();
    const timer = window.setInterval(() => void load(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, []);

  const history = stats?.history;

  return (
    <>
      {error && (
        <div className="banner banner-error" role="alert">
          Failed to load /stats: {error}
        </div>
      )}

      {!stats ? (
        <p className="muted">Loading overview…</p>
      ) : (
        <>
          <section className="section">
            <div className="section-head">
              <h2>Since restart</h2>
              <p className="section-note">
                In-memory counters for the current process.
              </p>
            </div>
            <div className="cards">
              <Metric
                label="Requests"
                value={formatNumber(stats.requests)}
                help="Proxy and /compress requests handled since restart."
              />
              <Metric
                label="Blocks compressed"
                value={formatNumber(stats.blocks)}
                help="Conversation blocks summarized since restart."
              />
              <Metric
                label="Cache hits"
                value={formatNumber(stats.hits)}
                help="Summaries reused from the SQLite cache since restart."
              />
              <Metric
                label="Errors"
                value={formatNumber(stats.errors)}
                help="Summarizer failures since restart."
              />
              <Metric
                label="Cached summaries"
                value={formatNumber(stats.cache.n)}
                help="Rows currently stored in the compressed cache."
              />
            </div>
          </section>

          <section className="section">
            <div className="section-head">
              <h2>Lifetime history (persisted)</h2>
              <p className="section-note">
                Aggregated from SQLite history and survives restarts. Token
                counts are estimates (~).
              </p>
            </div>
            <div className="cards">
              <Metric
                label="Requests recorded"
                value={formatNumber(history?.requests ?? 0)}
                help="Compression requests written to history."
              />
              <Metric
                label="Requests compacted"
                value={formatNumber(history?.compacted ?? 0)}
                help="Requests whose forwarded body differed from the original."
              />
              <Metric
                label="History errors"
                value={formatNumber(history?.errors ?? 0)}
                help="History records that recorded an error."
              />
              <Metric
                label="Estimated tokens before"
                value={formatTokens(history?.before_tokens ?? 0)}
                help="Sum of estimated original token counts."
              />
              <Metric
                label="Estimated tokens after"
                value={formatTokens(history?.after_tokens ?? 0)}
                help="Sum of estimated forwarded token counts."
              />
              <Metric
                label="Total processing time"
                value={formatDuration(history?.duration_ms ?? 0)}
                help="Time to compression completion or upstream headers; excludes streamed response time."
              />
            </div>
          </section>
        </>
      )}
    </>
  );
}

/* History ----------------------------------------------------------------- */

function HistoryPanel() {
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [changed, setChanged] = useState<ChangedFilter>("all");
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [next, setNext] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);
  const olderLoaded = useRef(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  const buildUrl = useCallback(
    (before?: number) => {
      const params = new URLSearchParams();
      params.set("limit", "50");
      if (before !== undefined) params.set("before", String(before));
      if (changed !== "all") params.set("changed", changed);
      if (debouncedSearch.trim()) params.set("search", debouncedSearch.trim());
      return `/api/history?${params.toString()}`;
    },
    [changed, debouncedSearch],
  );

  useEffect(() => {
    let active = true;

    let newest: number | null = null;
    let polling = false;
    const loadFirst = async () => {
      setLoadingOlder(false);
      try {
        const data = await getJson<HistoryResponse>(buildUrl());
        if (!active) return;
        setItems(data.items);
        setTotal(data.total);
        setNext(data.next);
        olderLoaded.current = false;
        newest = data.items[0]?.id ?? null;
        setError(null);
      } catch (loadError) {
        if (active) setError(messageOf(loadError));
      }
    };

    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        let data = await getJson<HistoryResponse>(buildUrl());
        const incoming = [...data.items];
        const latest = data.items[0]?.id ?? newest;
        // Fill gaps when more than one page arrives between polls.
        while (
          active &&
          newest !== null &&
          data.next !== null &&
          data.items[data.items.length - 1]?.id > newest
        ) {
          data = await getJson<HistoryResponse>(buildUrl(data.next));
          incoming.push(...data.items);
        }
        if (!active) return;
        newest = latest;
        setItems((previous) => mergeHistory(previous, incoming));
        setTotal(data.total);
        if (!olderLoaded.current) setNext(data.next);
        setError(null);
      } catch (pollError) {
        if (active) setError(messageOf(pollError));
      } finally {
        polling = false;
      }
    };

    void loadFirst();
    const timer = window.setInterval(() => void poll(), 5000);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [buildUrl]);

  const activeQuery = useRef(buildUrl);
  activeQuery.current = buildUrl;
  const loadOlder = async () => {
    const query = buildUrl;
    if (next === null) return;
    setLoadingOlder(true);
    try {
      const data = await getJson<HistoryResponse>(buildUrl(next));
      if (activeQuery.current !== query) return;
      olderLoaded.current = true;
      setItems((previous) => mergeHistory(previous, data.items));
      setTotal(data.total);
      setNext(data.next);
      setError(null);
    } catch (loadError) {
      if (activeQuery.current === query) setError(messageOf(loadError));
    } finally {
      if (activeQuery.current === query) setLoadingOlder(false);
    }
  };

  useEffect(() => {
    if (detailId === null) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDetailId(null);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [detailId]);

  return (
    <>
      {error && (
        <div className="banner banner-error" role="alert">
          Failed to load history: {error}
        </div>
      )}

      <div className="banner banner-warning">
        History stores full request payloads locally in SQLite. Sensitive
        content is retained with no automatic pruning. Recording starts at this
        deployment; earlier unrecorded payloads cannot be recovered.
      </div>

      <div className="toolbar">
        <div className="field">
          <label htmlFor="history-search">Search route or model</label>
          <input
            id="history-search"
            type="search"
            placeholder="e.g. openai/gpt or claude"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="history-changed">Changed</label>
          <select
            id="history-changed"
            value={changed}
            onChange={(event) =>
              setChanged(event.target.value as ChangedFilter)
            }
          >
            <option value="all">All requests</option>
            <option value="yes">Changed only</option>
            <option value="no">Unchanged only</option>
          </select>
        </div>
        <p className="field-help">
          Showing {formatNumber(items.length)} of {formatNumber(total)} matching
          records. Newest first; use “Load older” for anything beyond the first
          page.
        </p>
      </div>

      <div className="table-wrap">
        <table>
          <caption className="sr-only">Compression history</caption>
          <thead>
            <tr>
              <th scope="col">When</th>
              <th scope="col">Route</th>
              <th scope="col">Model</th>
              <th scope="col">Status</th>
              <th scope="col">Changed</th>
              <th scope="col">Tokens (est.)</th>
              <th scope="col">Duration</th>
              <th scope="col">Error</th>
            </tr>
          </thead>
          <tbody>
            {items.length === 0 ? (
              <tr>
                <td colSpan={8} className="empty-state">
                  No history records match the current filters.
                </td>
              </tr>
            ) : (
              items.map((item) => (
                <tr
                  key={item.id}
                  className={item.changed ? "row-changed" : undefined}
                  tabIndex={0}
                  aria-label={`Inspect history entry ${item.id}`}
                  onClick={() => setDetailId(item.id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") setDetailId(item.id);
                  }}
                >
                  <td className="nowrap-cell">{formatTime(item.created_at)}</td>
                  <td className="mono">{item.route}</td>
                  <td className="mono">{item.model}</td>
                  <td>{item.status || "pending"}</td>
                  <td>
                    <span
                      className={
                        item.changed ? "badge badge-ok" : "badge badge-muted"
                      }
                    >
                      {item.changed ? "yes" : "no"}
                    </span>
                  </td>
                  <td className="nowrap-cell">
                    {formatTokens(item.before_tokens)} →{" "}
                    {formatTokens(item.after_tokens)}
                  </td>
                  <td className="nowrap-cell">
                    {formatDuration(item.duration_ms)}
                  </td>
                  <td>
                    {item.error ? (
                      <span className="badge badge-error">error</span>
                    ) : (
                      "—"
                    )}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
        {next !== null && (
          <div className="pagination">
            <button
              type="button"
              className="btn"
              onClick={() => void loadOlder()}
              disabled={loadingOlder}
            >
              {loadingOlder ? "Loading…" : "Load older"}
            </button>
          </div>
        )}
      </div>

      {detailId !== null && (
        <HistoryDetailModal id={detailId} onClose={() => setDetailId(null)} />
      )}
    </>
  );
}

/* History detail ---------------------------------------------------------- */

function HistoryDetailModal({
  id,
  onClose,
}: {
  id: number;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<HistoryDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [wrap, setWrap] = useState(false);
  const dialog = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const bodyOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialog.current?.querySelector<HTMLButtonElement>("button")?.focus();
    const trap = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const controls = [
        ...(dialog.current?.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), a[href], [tabindex="0"]',
        ) ?? []),
      ];
      const first = controls[0],
        last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => {
      document.removeEventListener("keydown", trap);
      document.body.style.overflow = bodyOverflow;
      previous?.focus();
    };
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    setDetail(null);

    getJson<HistoryDetail>(`/api/history/${id}`)
      .then((data) => {
        if (active) {
          setDetail(data);
          setLoading(false);
        }
      })
      .catch((loadError) => {
        if (active) {
          setError(messageOf(loadError));
          setLoading(false);
        }
      });

    return () => {
      active = false;
    };
  }, [id]);

  const download = () => {
    if (!detail) return;
    const payload = {
      metadata: {
        id: detail.id,
        created_at: detail.created_at,
        route: detail.route,
        model: detail.model,
        status: detail.status,
        changed: detail.changed,
        before_tokens: detail.before_tokens,
        after_tokens: detail.after_tokens,
        duration_ms: detail.duration_ms,
        error: detail.error,
      },
      original: detail.original,
      compressed: detail.compressed,
      changes: detail.changes,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `history-${detail.id}.json`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <div
        className="modal"
        ref={dialog}
        role="dialog"
        aria-modal="true"
        aria-label={`History entry ${id}`}
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-header">
          <h2>History entry #{id}</h2>
          <button
            type="button"
            className="close-btn"
            aria-label="Close"
            onClick={onClose}
          >
            ×
          </button>
        </div>

        <div className="modal-body">
          {loading && <p className="muted">Loading detail…</p>}

          {error && (
            <div className="banner banner-error" role="alert">
              Failed to load /api/history/{id}: {error}
            </div>
          )}

          {detail && !loading && !error && (
            <>
              <div className="meta-grid">
                <div className="meta-item">
                  <dt>Created</dt>
                  <dd>{formatTime(detail.created_at)}</dd>
                </div>
                <div className="meta-item">
                  <dt>Route</dt>
                  <dd className="mono">{detail.route}</dd>
                </div>
                <div className="meta-item">
                  <dt>Model</dt>
                  <dd className="mono">{detail.model}</dd>
                </div>
                <div className="meta-item">
                  <dt>Status</dt>
                  <dd>{detail.status}</dd>
                </div>
                <div className="meta-item">
                  <dt>Changed</dt>
                  <dd>{detail.changed ? "yes" : "no"}</dd>
                </div>
                <div className="meta-item">
                  <dt>Tokens (est.)</dt>
                  <dd>
                    {formatTokens(detail.before_tokens)} →{" "}
                    {formatTokens(detail.after_tokens)}
                  </dd>
                </div>
                <div className="meta-item">
                  <dt>Duration</dt>
                  <dd>{formatDuration(detail.duration_ms)}</dd>
                </div>
                <div className="meta-item">
                  <dt>Error</dt>
                  <dd>{detail.error ?? "—"}</dd>
                </div>
              </div>

              <div className="toolbar">
                <label className="field checkbox-field">
                  <input
                    type="checkbox"
                    checked={wrap}
                    onChange={(event) => setWrap(event.target.checked)}
                  />
                  <span>Wrap long lines</span>
                </label>
                <button
                  type="button"
                  className="btn"
                  onClick={download}
                  disabled={!detail}
                >
                  Download full JSON
                </button>
              </div>

              <section className="detail-block">
                <h3>Changes ({detail.changes?.length ?? 0})</h3>
                {!detail.changes || detail.changes.length === 0 ? (
                  <p className="muted">
                    No individual changes were recorded for this request.
                  </p>
                ) : (
                  detail.changes.map((change, index) => (
                    <div
                      className="change-card"
                      key={`${change.kind}-${change.source_hash}-${index}`}
                    >
                      <div className="change-head">
                        <strong>{change.kind}</strong>
                        <span className="badge badge-muted">
                          source {change.source_hash.slice(0, 12)}
                        </span>
                        <span
                          className={
                            change.cached
                              ? "badge badge-ok"
                              : "badge badge-muted"
                          }
                        >
                          {change.cached ? "cache hit" : "generated"}
                        </span>
                        {change.fallback && (
                          <span className="badge badge-warn">fallback</span>
                        )}
                      </div>
                      <div className="change-values">
                        <div>
                          <h4>Before</h4>
                          <pre className={`json-block ${wrap ? "wrap" : ""}`}>
                            {pretty(change.before)}
                          </pre>
                        </div>
                        <div>
                          <h4>After</h4>
                          <pre className={`json-block ${wrap ? "wrap" : ""}`}>
                            {pretty(change.after)}
                          </pre>
                        </div>
                      </div>
                    </div>
                  ))
                )}
              </section>

              <section className="detail-block">
                <h3>Full original JSON</h3>
                <pre className={`json-block ${wrap ? "wrap" : ""}`}>
                  {detail.original === null ? "null" : pretty(detail.original)}
                </pre>
              </section>

              <section className="detail-block">
                <h3>Full forwarded JSON</h3>
                <pre className={`json-block ${wrap ? "wrap" : ""}`}>
                  {detail.compressed === null
                    ? "null"
                    : pretty(detail.compressed)}
                </pre>
              </section>

              <section className="detail-block">
                <h3>Unified diff (complete)</h3>
                <DiffView
                  original={detail.original}
                  compressed={detail.compressed}
                  wrap={wrap}
                />
              </section>
            </>
          )}
        </div>

        <div className="modal-footer">
          <button type="button" className="btn" onClick={onClose}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}

function DiffView({
  original,
  compressed,
  wrap,
}: {
  original: unknown | null;
  compressed: unknown | null;
  wrap: boolean;
}) {
  if (
    original === null ||
    original === undefined ||
    compressed === null ||
    compressed === undefined
  ) {
    return (
      <p className="muted">
        Diff unavailable: this record has no stored original or forwarded
        payload.
      </p>
    );
  }

  const parts = diffLines(pretty(original), pretty(compressed));
  const rows: DiffRow[] = [];
  let oldNo = 1;
  let newNo = 1;

  for (const part of parts) {
    const isAdd = Boolean(part.added);
    const isDel = Boolean(part.removed);
    const lines = part.value.split("\n");
    if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();

    for (const text of lines) {
      if (isAdd) {
        rows.push({ oldNo: null, newNo: newNo++, type: "add", text });
      } else if (isDel) {
        rows.push({ oldNo: oldNo++, newNo: null, type: "del", text });
      } else {
        rows.push({ oldNo: oldNo++, newNo: newNo++, type: "ctx", text });
      }
    }
  }

  return (
    <div
      className={`diff ${wrap ? "wrap" : "nowrap"}`}
      role="table"
      aria-label="Unified diff"
    >
      {rows.map((row, index) => (
        <div className={`diff-line diff-${row.type}`} role="row" key={index}>
          <span className="diff-gutter" aria-hidden="true">
            {row.oldNo ?? ""}
          </span>
          <span className="diff-gutter" aria-hidden="true">
            {row.newNo ?? ""}
          </span>
          <span className="diff-sign" aria-hidden="true">
            {row.type === "add" ? "+" : row.type === "del" ? "-" : " "}
          </span>
          <span className="diff-text">{row.text}</span>
        </div>
      ))}
    </div>
  );
}

/* Settings ---------------------------------------------------------------- */

function SettingsPanel() {
  const [numbers, setNumbers] = useState<Record<NumericKey, string> | null>(
    null,
  );
  const [prompts, setPrompts] = useState<Record<PromptKey, string> | null>(
    null,
  );
  const [model, setModel] = useState<ModelId | null>(null);
  const [defaults, setDefaults] = useState<Settings | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const applySettings = (settings: Settings) => {
    setNumbers(toNumberStrings(settings));
    setPrompts({
      blockPrompt: settings.blockPrompt,
    });
    setModel(settings.model);
  };

  useEffect(() => {
    let active = true;
    getJson<SettingsResponse>("/api/settings")
      .then((data) => {
        if (!active) return;
        applySettings(data.settings);
        setDefaults(data.defaults);
        setLoaded(true);
        setError(null);
      })
      .catch((loadError) => {
        if (!active) return;
        setError(messageOf(loadError));
        setLoaded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  const save = async () => {
    if (!numbers || !prompts || !model) return;
    const { payload, error: validationError } = buildSettingsPayload(
      numbers,
      prompts,
      model,
    );
    if (!payload) {
      setError(validationError ?? "Invalid settings.");
      setNotice(null);
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const data = await putJson<SettingsResponse>("/api/settings", payload);
      applySettings(data.settings);
      setDefaults(data.defaults);
      setNotice("Settings saved to SQLite.");
    } catch (saveError) {
      setError(messageOf(saveError));
    } finally {
      setSaving(false);
    }
  };

  const reset = () => {
    if (!defaults) return;
    applySettings(defaults);
    setError(null);
    setNotice("Form reset to .env defaults. Click Save to persist them.");
  };

  return (
    <>
      {error && (
        <div className="banner banner-error" role="alert">
          {error}
        </div>
      )}
      {notice && <div className="banner banner-ok">{notice}</div>}

      <div className="banner banner-warning">
        Settings are persisted in SQLite and take effect for future requests.
        Credentials, upstream URLs, routes, and the listen address cannot be
        edited here; change those in the .env file.
      </div>

      {!loaded || !numbers || !prompts || !model ? (
        <p className="muted">Loading settings…</p>
      ) : (
        <>
          <div className="settings-grid">
            <div className="field">
              <label htmlFor="setting-model">Summarizer model</label>
              <select
                id="setting-model"
                value={model}
                onChange={(event) => setModel(event.target.value as ModelId)}
              >
                {MODEL_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
              <span className="field-help">
                Summarizer used for block compression.
              </span>
            </div>
            {NUMERIC_FIELDS.map((field) => (
              <div className="field" key={field.key}>
                <label htmlFor={`setting-${field.key}`}>
                  {field.label}
                  {field.unit ? ` (${field.unit})` : ""}
                </label>
                <input
                  id={`setting-${field.key}`}
                  type="number"
                  inputMode="decimal"
                  min={field.min}
                  max={field.max}
                  step={field.step ?? 1}
                  value={numbers[field.key]}
                  onChange={(event) =>
                    setNumbers((previous) =>
                      previous
                        ? { ...previous, [field.key]: event.target.value }
                        : previous,
                    )
                  }
                />
                {field.key === "tokens" && (
                  <div
                    className="prefill-buttons"
                    style={{
                      display: "flex",
                      gap: "6px",
                      marginTop: "6px",
                      flexWrap: "wrap",
                    }}
                  >
                    {[
                      { label: "32k", value: 32000 },
                      { label: "64k", value: 64000 },
                      { label: "96k", value: 96000 },
                      { label: "128k", value: 128000 },
                    ].map((preset) => (
                      <button
                        key={preset.label}
                        type="button"
                        className="btn btn-small"
                        onClick={() =>
                          setNumbers((previous) =>
                            previous
                              ? { ...previous, tokens: String(preset.value) }
                              : previous,
                          )
                        }
                      >
                        {preset.label}
                      </button>
                    ))}
                  </div>
                )}
                <span className="field-help">
                  Range {field.min}
                  {field.max !== undefined ? `–${field.max}` : "+"}.{" "}
                  {field.help}
                </span>
              </div>
            ))}
          </div>

          <div className="prompt-grid">
            <div className="field">
              <label htmlFor="setting-blockPrompt">
                Block summarizer prompt
              </label>
              <textarea
                id="setting-blockPrompt"
                value={prompts.blockPrompt}
                onChange={(event) =>
                  setPrompts((previous) =>
                    previous
                      ? { ...previous, blockPrompt: event.target.value }
                      : previous,
                  )
                }
              />
              <span className="field-help">
                System prompt used when summarizing conversation blocks. The
                word hint is appended to the system prompt automatically. Each
                uncached block gets up to three 10-second attempts. All eligible
                blocks launch together; downstream waits for them.
              </span>
            </div>
          </div>

          <div className="settings-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={() => void save()}
              disabled={saving}
            >
              {saving ? "Saving…" : "Save settings"}
            </button>
            <button
              type="button"
              className="btn"
              onClick={reset}
              disabled={!defaults || saving}
            >
              Reset to .env defaults
            </button>
            <p className="field-help">
              Nothing is saved automatically. Reset fills the form with the .env
              defaults without persisting them.
            </p>
          </div>
        </>
      )}
    </>
  );
}
