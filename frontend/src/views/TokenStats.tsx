import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { api, isMissingEndpoint, type TokenBySource, type TokenRecent } from "../api";
import { DataTable, Panel, StatCard } from "../components";
import { ago, clockTime, fmt, millionTokens, secondsFromMs } from "../format";

const localNote =
  "Local model usage from the inference server. These counters cover local model calls only; router token savings and DeepSeek cost live under Context.";

export function TokenStatsView() {
  const query = useQuery({
    queryKey: ["token-stats"],
    queryFn: api.tokenStats,
    refetchInterval: 5000,
    retry: false,
  });

  const sourceColumns = useMemo<ColumnDef<TokenBySource, any>[]>(
    () => [
      { id: "source", accessorKey: "source", header: "Source", size: 160, cell: ({ row }) => row.original.source || "—" },
      { id: "requests", accessorKey: "requests", header: "Requests", size: 100, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      { id: "input", accessorKey: "input_tokens", header: "Input", size: 110, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      { id: "output", accessorKey: "output_tokens", header: "Output", size: 110, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      { id: "reasoning", accessorKey: "reasoning_tokens", header: "Reasoning", size: 110, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      { id: "cached", accessorKey: "cached_tokens", header: "Cached", size: 110, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      {
        id: "elapsed",
        accessorFn: (row) => row.elapsed_ms,
        header: "Avg elapsed",
        size: 120,
        meta: { align: "right" },
        cell: ({ row }) => secondsFromMs(row.original.requests ? row.original.elapsed_ms / row.original.requests : 0),
      },
    ],
    [],
  );

  const recentColumns = useMemo<ColumnDef<TokenRecent, any>[]>(
    () => [
      { id: "seq", accessorKey: "seq", header: "#", size: 70, meta: { align: "right" } },
      { id: "started", accessorKey: "started", header: "Started", size: 92, cell: ({ row }) => <span title={clockTime(row.original.started)}>{clockTime(row.original.started)}</span> },
      { id: "source", accessorKey: "source", header: "Source", size: 120 },
      { id: "model", accessorKey: "model", header: "Model", size: 180, cell: ({ row }) => row.original.model || "—" },
      { id: "status", accessorKey: "status", header: "Status", size: 90 },
      { id: "input", accessorKey: "input_tokens", header: "Input", size: 100, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      { id: "output", accessorKey: "output_tokens", header: "Output", size: 100, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      { id: "reasoning", accessorKey: "reasoning_tokens", header: "Reasoning", size: 100, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      { id: "cached", accessorKey: "cached_tokens", header: "Cached", size: 100, meta: { align: "right" }, cell: ({ getValue }) => fmt(getValue() as number) },
      { id: "elapsed", accessorKey: "elapsed_ms", header: "Elapsed", size: 100, meta: { align: "right" }, cell: ({ getValue }) => secondsFromMs(getValue() as number) },
      { id: "age", accessorFn: (row) => row.started, header: "Age", size: 80, cell: ({ row }) => <span className="text-muted">{ago(row.original.started)}</span> },
    ],
    [],
  );

  if (query.isLoading) {
    return <p className="px-3 py-6 text-muted">loading local model usage…</p>;
  }

  if (query.error != null) {
    return (
      <Panel title="Local model usage">
        <p className="px-3 py-6 text-muted">
          {isMissingEndpoint(query.error)
            ? "this backend does not expose /token-stats, so local model usage cannot be read."
            : "local model usage could not be loaded from /token-stats."}
        </p>
        <p className="px-3 pb-6 text-[12.5px] text-muted">{localNote}</p>
      </Panel>
    );
  }

  const data = query.data;
  if (!data) return null;
  const totals = data.totals;

  return (
    <div className="space-y-2">
      <p className="border border-line bg-panel px-3 py-1.5 text-[12.5px] text-muted">{localNote}</p>

      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        <StatCard label="Local requests" value={fmt(totals.requests)} detail={`${data.by_source.length} source${data.by_source.length === 1 ? "" : "s"}`} tone="accent" />
        <StatCard label="Input tokens" value={millionTokens(totals.input_tokens)} detail="prompt tokens sent to local models" />
        <StatCard label="Output tokens" value={millionTokens(totals.output_tokens)} detail="completion tokens produced" />
        <StatCard label="Reasoning tokens" value={millionTokens(totals.reasoning_tokens)} detail="reasoning tokens reported" />
        <StatCard label="Cached tokens" value={millionTokens(totals.cached_tokens)} detail="prompt tokens served from cache" />
        <StatCard label="Elapsed" value={secondsFromMs(totals.elapsed_ms)} detail={`avg ${secondsFromMs(totals.requests ? totals.elapsed_ms / totals.requests : 0)} per request`} />
      </div>

      <Panel title="Local usage by source">
        <DataTable columns={sourceColumns} data={data.by_source} empty="no local usage recorded" minWidth={760} />
      </Panel>

      <Panel title={`Recent local calls · last ${fmt(data.recent.length)}`}>
        <DataTable columns={recentColumns} data={data.recent} initialSorting={[{ id: "seq", desc: true }]} empty="no local calls recorded" minWidth={1000} />
      </Panel>
    </div>
  );
}
