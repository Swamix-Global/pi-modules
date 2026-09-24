import { useMemo } from "react";
import type { ColumnDef } from "@tanstack/react-table";
import type { RecentRequest, Stats } from "../api";
import { DataTable, Distribution, Panel, StatCard, ThroughputChart } from "../components";
import { ago, clockTime, dateTime, fmt, pct, secondsFromMs, thousandTokens, usd, usd0 } from "../format";

function ResultPill({ result }: { result: string }) {
  const tone =
    result === "passed"
      ? "bg-[#12301f] text-pos"
      : result === "compacted"
        ? "bg-[#3a2a12] text-[#d7a06a]"
        : "bg-[#3a1414] text-neg";
  return <span className={`inline-block px-1 py-0.5 text-[10.5px] ${tone}`}>{result}</span>;
}

export function TrafficStatsView({ stats }: { stats: Stats }) {
  const window = stats.window ?? { size: 0 };
  const recent = stats.recent ?? [];

  const columns = useMemo<ColumnDef<RecentRequest, any>[]>(
    () => [
      {
        id: "at",
        accessorFn: (row) => row.at,
        header: "Time",
        size: 92,
        cell: ({ row }) => <span title={dateTime(row.original.at)}>{clockTime(row.original.at)}</span>,
      },
      { id: "route", accessorKey: "route", header: "Route", size: 110 },
      { id: "model", accessorKey: "model", header: "Model", size: 170, cell: ({ row }) => row.original.model || "—" },
      { id: "provider", accessorKey: "provider", header: "Condenser", size: 100, cell: ({ row }) => row.original.provider || "—" },
      { id: "result", accessorKey: "result", header: "Result", size: 100, cell: ({ getValue }) => <ResultPill result={String(getValue())} /> },
      { id: "status", accessorKey: "status", header: "HTTP", size: 60, meta: { align: "right" }, cell: ({ getValue }) => (getValue() == null ? "—" : getValue()) },
      {
        id: "tokens",
        accessorFn: (row) => row.sent_tokens,
        header: "Sent / raw",
        size: 120,
        meta: { align: "right" },
        cell: ({ row }) => `${thousandTokens(row.original.sent_tokens)} / ${thousandTokens(row.original.raw_tokens)}`,
      },
      { id: "ms", accessorKey: "ms", header: "Seconds", size: 80, meta: { align: "right" }, cell: ({ getValue }) => secondsFromMs(getValue() as number | null) },
      { id: "age", accessorFn: (row) => row.at, header: "Age", size: 80, cell: ({ row }) => <span className="text-muted">{ago(row.original.at)}</span> },
      {
        id: "net",
        accessorFn: (row) => row.cost?.net_usd ?? -Infinity,
        header: "Net",
        size: 100,
        meta: { align: "right" },
        cell: ({ row }) => {
          const net = row.original.cost?.net_usd;
          if (net == null) return <span className="text-muted">—</span>;
          return <span className={net >= 0 ? "text-pos" : "text-neg"}>{usd(net)}</span>;
        },
      },
    ],
    [],
  );

  return (
    <div className="space-y-2">
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard
          label="Throughput · last 100"
          value={`${fmt(window.requests_per_min ?? 0)}`}
          detail={`req/min over ${fmt(window.span_s ?? 0)} s`}
          tone="accent"
        />
        <StatCard
          label="Pass / compact / error"
          value={`${pct(window.pass_rate ?? 0, 0)} / ${pct(window.compact_rate ?? 0, 0)} / ${pct(window.error_rate ?? 0, 0)}`}
          detail={`${fmt(window.size)} requests observed`}
        />
        <StatCard label="Latency p50 / p95" value={`${secondsFromMs(window.p50_ms)} / ${secondsFromMs(window.p95_ms)}`} detail={`avg ${secondsFromMs(window.avg_ms)} · max ${secondsFromMs(window.max_ms)}`} />
        <StatCard
          label="Window reclaimed"
          value={pct(window.saved_percent ?? 0)}
          detail={`${fmt(window.saved_tokens ?? 0)} of ${fmt(window.raw_tokens ?? 0)} tokens`}
          tone="pos"
        />
      </div>

      <div className="grid gap-2 xl:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <Panel title="Rolling throughput & latency · last 100">
          <ThroughputChart recent={recent} />
        </Panel>

        <div className="space-y-2">
          <Panel title="Window breakdown">
            <div className="space-y-2 px-3 py-2">
              <Distribution title="Results" map={window.results} />
              <Distribution title="Routes" map={window.routes} />
              <Distribution title="Models" map={window.models} />
              <Distribution title="Condensers" map={window.providers} />
            </div>
          </Panel>
          <Panel title="Window token budget">
            <div className="space-y-1 px-3 py-2 text-[12.5px]">
              <div>Tokens raw → sent: {fmt(window.raw_tokens ?? 0)} → {fmt(window.sent_tokens ?? 0)}</div>
              <div>Net saved: {window.net_usd == null ? "—" : `${usd(window.net_usd)} (${pct(window.net_percent ?? 0)} of ${fmt(window.raw_usd ?? 0)} USD raw)`}</div>
              <div>Condenser spend: {usd0(window.condenser_usd ?? 0)}</div>
            </div>
          </Panel>
        </div>
      </div>

      <Panel title={`Request ledger · last ${fmt(recent.length)}`}>
        <DataTable
          columns={columns}
          data={recent}
          initialSorting={[{ id: "at", desc: true }]}
          empty="no router requests recorded"
          minWidth={940}
        />
      </Panel>
    </div>
  );
}
