import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api, type Cost, type RecentRequest, type Stats } from "../api";
import {
  DataTable,
  Metrics,
  Panel,
  ReclaimedChart,
  StatCard,
} from "../components";
import { DiffModal } from "../DiffModal";
import { ago, clockTime, dateTime, dur, fmt, millionTokens, pct, r1, secondsFromMs, thousandTokens, usd, usd0 } from "../format";

function ResultPill({ result }: { result: string }) {
  const tone =
    result === "passed"
      ? "bg-[#12301f] text-pos"
      : result === "compacted"
        ? "bg-[#3a2a12] text-[#d7a06a]"
        : "bg-[#3a1414] text-neg";
  return <span className={`inline-block px-1 py-0.5 text-[10.5px] ${tone}`}>{result}</span>;
}

function costLine(row: RecentRequest): string {
  const cost = row.cost ?? {};
  if (cost.estimated) {
    return `estimated from token counts, no upstream usage captured · raw est ${usd0(cost.raw_usd)} · upstream est ${usd0(
      cost.sent_usd,
    )} · net ${usd(cost.net_usd)} (${fmt(cost.net_percent)} %)`;
  }
  if (cost.sent_usd != null) {
    return `upstream ${usd0(cost.sent_usd)} (${fmt(cost.in)} in · ${fmt(cost.cached)} cached · ${fmt(cost.out)} out) · raw est ${usd0(
      cost.raw_usd,
    )} · condenser ${usd0(cost.condenser_usd)}${cost.condenser_calls ? ` (${cost.condenser_calls} call)` : ""} · net ${usd(
      cost.net_usd,
    )} (${fmt(cost.net_percent)} %)`;
  }
  if (cost.in != null) return `upstream ${fmt(cost.in)} in · ${fmt(cost.cached)} cached · ${fmt(cost.out)} out (no declared rate)`;
  return "";
}

function NetCell({ cost }: { cost: Cost | null }) {
  const net = cost?.net_usd;
  if (net == null) return <span className="text-muted">—</span>;
  return (
    <span className={net >= 0 ? "text-pos" : "text-neg"} title={cost?.net_percent != null ? `${cost.net_percent} % of raw` : undefined}>
      {usd(net)}
    </span>
  );
}

export function ContextView({ stats }: { stats: Stats }) {
  const [modalId, setModalId] = useState<number | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [showRaw, setShowRaw] = useState(false);

  const detailQuery = useQuery({
    queryKey: ["request", modalId],
    queryFn: () => api.request(modalId as number),
    enabled: modalId != null,
    retry: false,
    staleTime: 0,
  });

  const toggleExpanded = (id: number) =>
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const columns = useMemo<ColumnDef<RecentRequest, any>[]>(
    () => [
      {
        id: "at",
        accessorFn: (row) => row.at,
        header: "Time",
        size: 92,
        cell: ({ row }) => <span title={dateTime(row.original.at)}>{clockTime(row.original.at)}</span>,
      },
      { id: "route", accessorKey: "route", header: "Route", size: 110, cell: ({ getValue }) => <span title={String(getValue())}>{String(getValue())}</span> },
      {
        id: "model",
        accessorKey: "model",
        header: "Model / detail",
        size: 200,
        cell: ({ row }) => <span className="text-muted" title={row.original.model || undefined}>{row.original.model || "—"}</span>,
      },
      {
        id: "tokens",
        accessorFn: (row) => row.sent_tokens,
        header: "Sent / raw",
        size: 120,
        meta: { align: "right" },
        cell: ({ row }) => `${thousandTokens(row.original.sent_tokens)} / ${thousandTokens(row.original.raw_tokens)}`,
      },
      { id: "result", accessorKey: "result", header: "Result", size: 100, cell: ({ getValue }) => <ResultPill result={String(getValue())} /> },
      { id: "status", accessorKey: "status", header: "HTTP", size: 60, meta: { align: "right" }, cell: ({ getValue }) => (getValue() == null ? "—" : getValue()) },
      { id: "ms", accessorKey: "ms", header: "Seconds", size: 80, meta: { align: "right" }, cell: ({ getValue }) => secondsFromMs(getValue() as number | null) },
      { id: "age", accessorFn: (row) => row.at, header: "Age", size: 80, cell: ({ row }) => <span className="text-muted">{ago(row.original.at)}</span> },
      { id: "net", accessorFn: (row) => row.cost?.net_usd ?? -Infinity, header: "Net", size: 100, meta: { align: "right" }, cell: ({ row }) => <NetCell cost={row.original.cost} /> },
      {
        id: "inspect",
        header: "Diff",
        size: 56,
        enableSorting: false,
        meta: { align: "right" },
        cell: ({ row }) =>
          row.original.result === "compacted" ? (
            <button
              type="button"
              title="inspect the compaction diff"
              className="text-accent hover:underline"
              onClick={(event) => {
                event.stopPropagation();
                setModalId(row.original.id);
              }}
            >
              ⇄
            </button>
          ) : (
            <span className="text-muted" title="nothing compacted">
              —
            </span>
          ),
      },
    ],
    [],
  );

  const global = stats.global;
  const seen = (global?.raw_tokens ?? 0) > 0;
  const window = stats.window ?? { size: 0 };
  const buckets = stats.cache_buckets ?? {};
  const condenserCalls = stats.condenser_calls ?? 0;

  return (
    <div className="space-y-2">
      <div className="text-[12.5px] text-muted">{stats.model} condenser · compacts above {fmt(stats.token_limit)} tokens · {fmt(stats.summary_words)} words per block</div>

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Context reclaimed"
          value={seen ? pct(global.saved_percent) : "—"}
          detail={seen ? `${millionTokens(global.saved_tokens)} of ${millionTokens(global.raw_tokens)}, all time` : "no traffic yet"}
          tone="accent"
        />
        <StatCard
          label="Net saved"
          value={seen ? usd0(global.net_usd) : "—"}
          detail={seen ? `${pct(global.net_percent)} of ${usd0(global.raw_usd)} raw` : "no traffic yet"}
          tone="pos"
        />
        <StatCard label="Requests recorded" value={fmt(global.size)} detail={`${fmt(window.size)} in last-100 window`} />
        <StatCard label="Uptime" value={dur(stats.uptime_s)} detail={`started ${clockTime(stats.started)}`} />
      </div>

      <div className="grid gap-2 xl:grid-cols-[minmax(0,1fr)_minmax(0,2fr)]">
        <div className="space-y-2">
          <Panel title="Token billing">
            <Metrics
              rows={[
                ["Tokens raw → sent", `${millionTokens(global.raw_tokens)} → ${millionTokens(global.sent_tokens)} (all time)`],
                ["Reclaimed", `${millionTokens(global.saved_tokens)} (${pct(global.saved_percent)})`],
                ["Raw USD had it passed", usd0(global.raw_usd)],
                ["Net saved", `${usd(global.net_usd)} (${pct(global.net_percent)})`],
                ["Session tokens (estimated)", `${fmt(stats.input_tokens_raw_est)} → ${fmt(stats.input_tokens_sent_est)}`],
                ["Billed vs raw (window)", `${fmt(window.sent_tokens)} / ${fmt(window.raw_tokens)} tok · ${usd0(window.upstream_usd)} / ${usd0(window.raw_usd)}`],
                ["Token budget", fmt(stats.token_limit)],
              ]}
            />
          </Panel>

          <Panel title="Cache & blocks">
            <Metrics
              rows={[
                ["Blocks seen", fmt(stats.blocks_seen)],
                ["Blocks summarized", fmt(stats.blocks_summarized)],
                ["Fallback notes", fmt(stats.blocks_fallback)],
                ["Cache hits", fmt(stats.cache_hits)],
                ["Cached now", fmt(stats.cache_entries)],
                ["Hit rate", pct(stats.blocks_seen ? (100 * stats.cache_hits) / stats.blocks_seen : 0)],
                ...Object.entries(buckets).map(
                  ([bucket, count]) => [`Cached · ${bucket}`, fmt(count)] as [string, string],
                ),
              ]}
            />
          </Panel>

          <Panel title="Traffic">
            <Metrics
              rows={[
                ["Requests", fmt(stats.requests)],
                ["Passed through", fmt(stats.passed)],
                ["Compacted", fmt(stats.compacted)],
                ["Images passed", fmt(stats.images)],
                ["Live turn trimmed", fmt(stats.trimmed)],
                ["Still over budget", fmt(stats.overflow)],
                ["Pass / compact rate", `${pct(stats.requests ? (100 * stats.passed) / stats.requests : 0)} / ${pct(stats.requests ? (100 * stats.compacted) / stats.requests : 0)}`],
              ]}
            />
          </Panel>

          <Panel title="Condenser & runtime">
            <Metrics
              rows={[
                ["Account", stats.user.username],
                ["Proxy path", `${globalThis.location.origin}/t/${stats.user.api_token}`],
                ["Active backend", `${stats.provider} · ${stats.model}`],
                ["Calls total", fmt(condenserCalls)],
                ["DeepSeek calls", fmt(stats.condenser_calls_deepseek)],
                ["Avg latency", secondsFromMs(stats.condenser_avg_ms)],
                ["Latency total", secondsFromMs(stats.condenser_ms_total)],
                ["Tokens in / out", `${fmt(stats.condenser_tokens_in)} / ${fmt(stats.condenser_tokens_out)}`],
                ["Cost total", usd0(stats.condenser_cost_usd)],
                ["Errors", fmt(stats.condenser_errors)],
                ["Last compaction", stats.last_compaction ? clockTime(stats.last_compaction) : "never"],
                ["Chars / token", String(r1(stats.chars_per_token || 3.5))],
                ["DB", stats.cache_db],
                ["DB errors", fmt(stats.db_errors ?? 0)],
              ]}
            />
          </Panel>
        </div>

        <div className="space-y-2">
          <Panel title="Context reclaimed · 24 h per minute">
            <ReclaimedChart series={stats.series} now={stats.now} />
          </Panel>

          <Panel
            title={`Last 100 requests${window.size ? ` · ${fmt(window.size)} observed · ${fmt(window.requests_per_min ?? 0)} req/min` : ""}`}
            actions={
              <button
                type="button"
                aria-pressed={showRaw}
                onClick={() => setShowRaw((value) => !value)}
                className={`border border-line px-2 py-0.5 text-[11px] normal-case tracking-normal ${showRaw ? "text-accent" : "text-muted hover:text-body"}`}
              >
                {showRaw ? "Raw JSON" : "Detail"}
              </button>
            }
          >
            <DataTable
              columns={columns}
              data={stats.recent ?? []}
              initialSorting={[{ id: "at", desc: true }]}
              empty="no requests yet"
              onRowClick={(row) => toggleExpanded(row.id)}
              rowAriaLabel={(row) => `request ${row.id} on ${row.route}, ${row.result}, press to expand detail`}
              subRow={(row) =>
                expanded.has(row.id) ? (
                  <pre className="m-0 whitespace-pre-wrap break-words font-mono text-[11.5px] text-muted">
                    {costLine(row)}
                    {costLine(row) ? "\n" : ""}
                    {showRaw ? row.raw : row.detail || "no detail captured"}
                  </pre>
                ) : null
              }
            />
          </Panel>
        </div>
      </div>

      {modalId != null && (
        <DiffModal
          detail={detailQuery.data}
          error={detailQuery.error}
          loading={detailQuery.isLoading}
          onClose={() => setModalId(null)}
        />
      )}
    </div>
  );
}
