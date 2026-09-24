import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { BUDGET_POINTS, OUTPUT_POINTS, SettingSlider, Tabs, type TabDef } from "./components";
import { clockTime, dur, syncClock } from "./format";
import { ContextView } from "./views/Context";
import { TokenStatsView } from "./views/TokenStats";
import { TrafficStatsView } from "./views/TrafficStats";

const TABS: TabDef[] = [
  { id: "context", label: "Context" },
  { id: "tokens", label: "Token Stats" },
  { id: "traffic", label: "Traffic Stats" },
];

export default function App() {
  const [tab, setTab] = useState("context");
  const queryClient = useQueryClient();
  const settings = useMutation({
    mutationFn: (change: { tokens?: number; summary_tokens?: number }) => api.setConfig(change),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["stats"] }),
  });

  const statsQuery = useQuery({
    queryKey: ["stats"],
    queryFn: api.stats,
    refetchInterval: 5000,
    retry: false,
  });

  const stats = statsQuery.data;
  // All relative ages and the 24 h chart window follow the router's own clock, never the browser's.
  if (stats) syncClock(stats.now);

  return (
    <main className="mx-auto max-w-[1800px] px-3.5 pb-8 pt-3">
      <header className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-line pb-3">
        <h1 className="text-[15px] font-bold tracking-[0.06em]">
          <span className="mr-1 text-accent" aria-hidden="true">
            ⇄
          </span>
          CTX ROUTER
        </h1>
        <p className="min-w-[200px] flex-1 text-[12.5px] text-muted">
          {stats
            ? `${stats.provider} · ${stats.model} · uptime ${dur(stats.uptime_s)}`
            : "connecting to the context router…"}
        </p>
        {stats && <div className="flex flex-wrap gap-2" aria-label="Global settings">
          <SettingSlider label="Compaction threshold" points={BUDGET_POINTS} value={stats.token_limit}
            onCommit={(tokens) => settings.mutate({ tokens })} disabled={settings.isPending} />
          <SettingSlider label="Block output length" points={OUTPUT_POINTS} value={stats.summary_tokens}
            onCommit={(summary_tokens) => settings.mutate({ summary_tokens })} disabled={settings.isPending} />
        </div>}
        {stats && <span className="text-[12.5px] text-muted">router clock {clockTime(stats.now)}</span>}
        <Tabs tabs={TABS} active={tab} onChange={setTab} />
      </header>

      {settings.isError && <p role="alert" className="py-2 text-neg">Could not save setting; refresh to retry.</p>}
      <div
        role="tabpanel"
        id={`panel-${tab}`}
        aria-labelledby={`tab-${tab}`}
        className="mt-2"
      >
        {statsQuery.isLoading && <p className="px-1 py-6 text-muted">loading counters…</p>}
        {statsQuery.error != null && !stats && (
          <p className="px-1 py-6 text-muted">the router /stats endpoint did not answer — retrying every 5 s.</p>
        )}
        {stats && tab === "context" && <ContextView stats={stats} />}
        {stats && tab === "traffic" && <TrafficStatsView stats={stats} />}
        {tab === "tokens" && <TokenStatsView />}
      </div>
    </main>
  );
}
