import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";
import { BUDGET_POINTS, SettingSlider, Tabs, type TabDef } from "./components";
import { clockTime, dur, syncClock } from "./format";
import { ContextView } from "./views/Context";
import { TokenStatsView } from "./views/TokenStats";
import { TrafficStatsView } from "./views/TrafficStats";
import { DebuggerView } from "./views/Debugger";

const TABS: TabDef[] = [
  { id: "context", label: "Context" },
  { id: "tokens", label: "Token Stats" },
  { id: "traffic", label: "Traffic Stats" },
  { id: "debugger", label: "Debugger" },
];

const TAB_PATHS: Record<string, string> = {
  context: "/dashboard",
  tokens: "/token-stats",
  traffic: "/dashboard/traffic",
  debugger: "/dashboard/debugger",
};

function tabFromPath(path: string): string {
  if (path === "/dashboard/debugger") return "debugger";
  if (path === "/token-stats") return "tokens";
  if (path === "/dashboard/traffic") return "traffic";
  return "context";
}

export default function App() {
  const [tab, setTab] = useState(() => tabFromPath(window.location.pathname));
  useEffect(() => {
    const onPopState = () => setTab(tabFromPath(window.location.pathname));
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);
  const navigateTab = (next: string) => {
    setTab(next);
    const path = TAB_PATHS[next];
    if (path && window.location.pathname !== path) window.history.pushState({}, "", path);
  };
  const queryClient = useQueryClient();
  const settings = useMutation({
    mutationFn: (change: { tokens?: number; summary_words?: number; provider?: string }) => api.setConfig(change),
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
          <div className="inline-flex overflow-hidden border border-line bg-panel" role="radiogroup" aria-label="Condenser provider">
            {stats.providers.map((choice) => {
              const selected = choice.provider === stats.provider;
              return <button key={choice.provider} type="button" role="radio" aria-checked={selected} disabled={!choice.available || settings.isPending}
                title={choice.available ? `${choice.label} · ${choice.model}` : choice.error || `${choice.label} unavailable`}
                onClick={() => settings.mutate({ provider: choice.provider })}
                className={`border-l border-line px-2.5 py-1.5 text-[12px] first:border-l-0 ${selected ? "bg-accentbg font-semibold text-accent" : "text-muted hover:text-body"} disabled:cursor-not-allowed disabled:opacity-40`}>
                {choice.label}
              </button>;
            })}
          </div>
        </div>}
        {stats && <span className="text-[12.5px] text-muted">router clock {clockTime(stats.now)}</span>}
        <Tabs tabs={TABS} active={tab} onChange={navigateTab} />
      </header>

      {settings.isError && <p role="alert" className="py-2 text-neg">Could not save setting; refresh to retry.</p>}
      <div
        role="tabpanel"
        id={`panel-${tab}`}
        aria-labelledby={`tab-${tab}`}
        className="mt-2"
      >
        {tab === "debugger" && <DebuggerView />}
        {tab !== "debugger" && statsQuery.isLoading && <p className="px-1 py-6 text-muted">loading counters…</p>}
        {tab !== "debugger" && statsQuery.error != null && !stats && (
          <p className="px-1 py-6 text-muted">the router /stats endpoint did not answer — retrying every 5 s.</p>
        )}
        {stats && tab === "context" && <ContextView stats={stats} />}
        {stats && tab === "traffic" && <TrafficStatsView stats={stats} />}
        {tab === "tokens" && <TokenStatsView />}
      </div>
    </main>
  );
}
