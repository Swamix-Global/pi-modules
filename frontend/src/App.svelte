<script>
  import { onMount } from "svelte";
  import {
    Chart,
    LineController,
    LineElement,
    PointElement,
    LinearScale,
    CategoryScale,
    Tooltip,
    Legend,
    Filler,
  } from "chart.js";

  Chart.register(
    LineController,
    LineElement,
    PointElement,
    LinearScale,
    CategoryScale,
    Tooltip,
    Legend,
    Filler,
  );

  /* ------------------------------------------------------------------ */
  /* Constants                                                           */
  /* ------------------------------------------------------------------ */

  const TABS = [
    { id: "overview", label: "Overview", icon: "fa-solid fa-chart-line" },
    { id: "requests", label: "Requests", icon: "fa-solid fa-clock-rotate-left" },
    { id: "settings", label: "Settings", icon: "fa-solid fa-sliders" },
  ];

  const MODEL_OPTIONS = [
    { value: "deepseek-flash", label: "DeepSeek Flash (low reasoning)" },
    { value: "gpt-6-luna", label: "Codex Luna 6 (Pi subscription)" },
  ];

  const NUMERIC_FIELDS = [
    {
      key: "tokens",
      label: "Context token budget",
      hint: "Soft threshold before blocks are condensed.",
      min: 1024,
      step: 1,
      unit: "tokens",
    },
    {
      key: "keepTurns",
      label: "Protected recent turns",
      hint: "Most recent turns never condensed.",
      min: 1,
      step: 1,
      unit: "turns",
    },
    {
      key: "maxTokens",
      label: "Maximum summary tokens",
      hint: "Upper bound for a single block summary.",
      min: 128,
      step: 1,
      unit: "tokens",
    },
  ];

  const REFRESH_MS = 5000;
  // Chart buckets per range must match backend/db.py stats() timeline buckets.
  const CHART_RANGES = {
    "24h": { label: "Last 24 hours", bucketMs: 300000, buckets: 288 },
    "7d": { label: "Last 7 days", bucketMs: 3600000, buckets: 168 },
    "30d": { label: "Last 30 days", bucketMs: 86400000, buckets: 30 },
  };
  const RANGE_OPTIONS = [
    { id: "24h", label: "24h" },
    { id: "7d", label: "7d" },
    { id: "30d", label: "30d" },
  ];
  const WINDOWS = [
    { id: "minute", label: "1 min" },
    { id: "hour", label: "1 hour" },
    { id: "day", label: "1 day" },
    { id: "all", label: "All time" },
  ];

  /* ------------------------------------------------------------------ */
  /* Helpers                                                             */
  /* ------------------------------------------------------------------ */

  function messageOf(error) {
    return error instanceof Error ? error.message : String(error);
  }

  async function readErrorDetail(response) {
    try {
      const body = await response.json();
      return body && typeof body.error === "string" ? body.error : "";
    } catch {
      return "";
    }
  }

  async function getJson(url) {
    const response = await fetch(url, {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) {
      const detail = await readErrorDetail(response);
      throw new Error(
        `${response.status} ${response.statusText}${detail ? `: ${detail}` : ""}`,
      );
    }
    return response.json();
  }

  async function putJson(url, body) {
    const response = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10000),
    });
    if (!response.ok) {
      const detail = await readErrorDetail(response);
      throw new Error(
        `${response.status} ${response.statusText}${detail ? `: ${detail}` : ""}`,
      );
    }
    return response.json();
  }

  function formatNumber(value) {
    return Number.isFinite(value) ? value.toLocaleString() : "—";
  }

  function formatRate(value) {
    if (value === null || !Number.isFinite(value)) return "—";
    if (value === 0) return "0";
    if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
    if (value >= 10_000) return `${Math.round(value / 1000)}k`;
    if (value >= 1_000) return `${(value / 1000).toFixed(1)}k`;
    return value < 100 ? value.toFixed(1) : `${Math.round(value)}`;
  }

  function formatTokenFlow(before, after) {
    if (!Number.isFinite(before) || !Number.isFinite(after) || before < 0 || after < 0)
      return "—";
    const change = before > 0 ? ((after - before) / before) * 100 : after === 0 ? 0 : null;
    const rounded = change === null ? null : Math.round(change * 10) / 10;
    const percent = rounded === null ? "—" : `${rounded > 0 ? "+" : ""}${rounded.toFixed(1)}%`;
    return `${(before / 1000).toFixed(1)} k → ${(after / 1000).toFixed(1)} k (${percent})`;
  }

  function formatDuration(ms) {
    if (!Number.isFinite(ms) || ms < 0) return "—";
    if (ms < 1000) return `${Math.round(ms)} ms`;
    const seconds = ms / 1000;
    if (seconds < 60) return `${seconds.toFixed(1)} s`;
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  }

  function formatTime(ms) {
    if (!Number.isFinite(ms)) return "—";
    return new Date(ms).toLocaleString(undefined, {
      year: "numeric",
      month: "numeric",
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function formatHour(ms) {
    return new Date(ms).toLocaleTimeString(undefined, {
      hour: "numeric",
      minute: "2-digit",
    });
  }

  function formatChartTick(ms) {
    return [
      new Date(ms).toLocaleDateString(undefined, { month: "short", day: "numeric" }),
      formatHour(ms),
    ];
  }

  function pretty(value) {
    if (value === undefined || value === null) return "null";
    return JSON.stringify(value, null, 2);
  }

  function urlParam(name) {
    return new URLSearchParams(window.location.search).get(name);
  }

  function setUrlParam(name, value) {
    const params = new URLSearchParams(window.location.search);
    if (value === null || value === undefined || value === "") params.delete(name);
    else params.set(name, String(value));
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      `${window.location.pathname}${query ? `?${query}` : ""}`,
    );
  }

  function chartConfig(range) {
    if (range === "7d") return CHART_RANGES["7d"];
    if (range === "30d") return CHART_RANGES["30d"];
    return CHART_RANGES["24h"];
  }

  function isChartRange(value) {
    return value === "24h" || value === "7d" || value === "30d";
  }

  function compileTimeline(timeline, serverNow, range) {
    const { bucketMs, buckets } = chartConfig(range);
    const now = Number.isFinite(serverNow) ? serverNow : Date.now();
    const end = Math.floor(now / bucketMs) * bucketMs;
    const start = end - (buckets - 1) * bucketMs;
    const map = new Map();
    for (const point of Array.isArray(timeline) ? timeline : []) {
      map.set(point.time, point);
    }
    const series = [];
    for (let t = start; t <= end; t += bucketMs) {
      series.push(
        map.get(t) ?? {
          time: t,
          requests: 0,
          compacted: 0,
          errors: 0,
          before_tokens: 0,
          after_tokens: 0,
        },
      );
    }
    return series;
  }

  function statusBadge(status) {
    if (status === 0)
      return {
        label: "in-flight",
        icon: "fa-solid fa-spinner fa-spin",
        cls: "border-amber-400/40 bg-amber-400/10 text-amber-600 dark:text-amber-300",
      };
    if (status >= 400)
      return {
        label: `error ${status}`,
        icon: "fa-solid fa-circle-exclamation",
        cls: "border-rose-400/40 bg-rose-400/10 text-rose-600 dark:text-rose-300",
      };
    if (status >= 300)
      return {
        label: `redirect ${status}`,
        icon: "fa-solid fa-arrow-right-arrow-left",
        cls: "border-sky-400/40 bg-sky-400/10 text-sky-600 dark:text-sky-300",
      };
    return {
      label: `ok ${status}`,
      icon: "fa-solid fa-circle-check",
      cls: "border-emerald-400/40 bg-emerald-400/10 text-emerald-600 dark:text-emerald-300",
    };
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      return false;
    }
  }

  function downloadText(filename, text) {
    const blob = new Blob([text], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  }

  /* ------------------------------------------------------------------ */
  /* Local state                                                         */
  /* ------------------------------------------------------------------ */

  let tab = $state(initialTab());
  let dark = $state(document.documentElement.classList.contains("dark"));

  // Overview / stats
  let stats = $state(null);
  let chartRange = $state(initialRange());
  let health = $state(null);
  let statsError = $state("");
  let disconnected = $state(false);
  let lastUpdated = $state(null);
  let refreshingStats = $state(false);
  let statsReloadPending = false;

  // Requests / history
  let history = $state([]);
  let total = $state(0);
  let nextCursor = $state(null);
  let beforeCursor = $state(null);
  let historyLoaded = $state(false);
  let historyLoading = $state(false);
  let historyError = $state("");
  let search = $state("");
  let changed = $state("all");
  let searchTimer;

  // Detail
  let detailId = $state(initialRequest());
  let detail = $state(null);
  let detailLoading = $state(false);
  let detailError = $state("");
  let notice = $state("");

  // Settings
  let settingsLoaded = $state(false);
  let settingsLoading = $state(false);
  let settingsError = $state("");
  let settingsSuccess = $state("");
  let settingsSaving = $state(false);
  let defaults = $state(null);
  let form = $state({
    tokens: "32000",
    keepTurns: "2",
    maxTokens: "8192",
    model: "deepseek-flash",
    blockPrompt: "",
  });

  // Chart
  let chartEl = $state(null);
  let chart = null;

  /* ------------------------------------------------------------------ */
  /* URL bootstrap                                                       */
  /* ------------------------------------------------------------------ */

  function initialTab() {
    const value = new URLSearchParams(window.location.search).get("tab");
    return ["overview", "requests", "settings"].includes(value)
      ? value
      : "overview";
  }

  function initialRange() {
    const value = new URLSearchParams(window.location.search).get("range");
    return isChartRange(value) ? value : "24h";
  }

  function initialRequest() {
    const value = Number(
      new URLSearchParams(window.location.search).get("request"),
    );
    return Number.isSafeInteger(value) && value > 0 ? value : null;
  }

  function selectTab(next) {
    tab = next;
    setUrlParam("tab", next === "overview" ? null : next);
    if (next === "requests" && !historyLoaded) loadHistory();
    if (next === "settings" && !settingsLoaded) loadSettings();
  }

  /* ------------------------------------------------------------------ */
  /* Theme                                                               */
  /* ------------------------------------------------------------------ */

  $effect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("ctx-theme", dark ? "dark" : "light");
  });

  /* ------------------------------------------------------------------ */
  /* Overview data                                                       */
  /* ------------------------------------------------------------------ */

  async function refreshStats() {
    if (refreshingStats) {
      statsReloadPending = true;
      return;
    }
    refreshingStats = true;
    const statsUrl = chartRange === "24h" ? "/stats" : `/stats?range=${chartRange}`;
    try {
      const [healthResponse, statsResponse] = await Promise.all([
        fetch("/health", { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(10000) }),
        fetch(statsUrl, { headers: { Accept: "application/json" }, signal: AbortSignal.timeout(10000) }),
      ]);
      if (!healthResponse.ok)
        throw new Error(`health ${healthResponse.status} ${healthResponse.statusText}`);
      if (!statsResponse.ok)
        throw new Error(`stats ${statsResponse.status} ${statsResponse.statusText}`);
      health = await healthResponse.json();
      stats = await statsResponse.json();
      statsError = "";
      disconnected = false;
      lastUpdated = Date.now();
    } catch (error) {
      statsError = messageOf(error);
      disconnected = true;
    } finally {
      refreshingStats = false;
      if (statsReloadPending) {
        statsReloadPending = false;
        refreshStats();
      }
    }
  }

  function selectChartRange(next) {
    if (!isChartRange(next) || next === chartRange) return;
    chartRange = next;
    setUrlParam("range", next === "24h" ? null : next);
    refreshStats();
  }

  onMount(() => {
    refreshStats();
    const timer = setInterval(() => {
      if (document.visibilityState === "hidden") return;
      refreshStats();
      if (tab === "requests" && beforeCursor == null) loadHistory();
    }, REFRESH_MS);

    if (detailId != null) {
      tab = "requests";
      setUrlParam("tab", "requests");
      loadHistory();
      loadDetail(detailId);
    } else if (tab === "requests") {
      loadHistory();
    }
    if (tab === "settings") loadSettings();

    return () => {
      clearInterval(timer);
      clearTimeout(searchTimer);
    };
  });

  /* ------------------------------------------------------------------ */
  /* Requests data                                                       */
  /* ------------------------------------------------------------------ */

  let historyReloadPending = false;

  async function loadHistory() {
    if (historyLoading) {
      historyReloadPending = true;
      return;
    }
    historyLoading = true;
    historyError = "";
    try {
      const params = new URLSearchParams({ limit: "50", changed });
      if (beforeCursor != null) params.set("before", String(beforeCursor));
      if (search.trim()) params.set("search", search.trim());
      const data = await getJson(`/api/history?${params.toString()}`);
      history = Array.isArray(data.items) ? data.items : [];
      total = Number.isFinite(data.total) ? data.total : history.length;
      nextCursor = data.next ?? null;
      historyLoaded = true;
      if (detailId != null && detail?.status === 0 && !detailLoading) loadDetail(detailId);
    } catch (error) {
      historyError = messageOf(error);
    } finally {
      historyLoading = false;
      if (historyReloadPending) {
        historyReloadPending = false;
        loadHistory();
      }
    }
  }

  function onSearchInput() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      beforeCursor = null;
      loadHistory();
    }, 300);
  }

  function onChangedChange() {
    beforeCursor = null;
    loadHistory();
  }

  function older() {
    if (nextCursor == null) return;
    beforeCursor = nextCursor;
    loadHistory();
  }

  function newest() {
    if (beforeCursor == null) return;
    beforeCursor = null;
    loadHistory();
  }

  function toggleRow(id) {
    if (detailId === id) {
      detailId = null;
      detail = null;
      detailError = "";
      setUrlParam("request", null);
      return;
    }
    detailId = id;
    setUrlParam("request", id);
    loadDetail(id);
  }

  async function loadDetail(id) {
    detailLoading = true;
    detailError = "";
    try {
      const result = await getJson(`/api/history/${id}`);
      if (detailId === id) detail = result;
    } catch (error) {
      if (detailId === id) {
        detail = null;
        detailError = messageOf(error);
      }
    } finally {
      if (detailId === id) detailLoading = false;
    }
  }

  /* ------------------------------------------------------------------ */
  /* Settings data                                                       */
  /* ------------------------------------------------------------------ */

  async function loadSettings() {
    if (settingsLoading) return;
    settingsLoading = true;
    settingsError = "";
    try {
      const data = await getJson("/api/settings");
      defaults = data.defaults ?? null;
      applySettings(data.settings);
      settingsLoaded = true;
    } catch (error) {
      settingsError = messageOf(error);
    } finally {
      settingsLoading = false;
    }
  }

  function applySettings(settings) {
    if (!settings) return;
    form = {
      tokens: String(settings.tokens ?? 32000),
      keepTurns: String(settings.keepTurns ?? 2),
      maxTokens: String(settings.maxTokens ?? 8192),
      model: settings.model === "gpt-6-luna" ? "gpt-6-luna" : "deepseek-flash",
      blockPrompt: settings.blockPrompt ?? "",
    };
  }

  async function saveSettings() {
    settingsError = "";
    settingsSuccess = "";
    const payload = {};
    for (const field of NUMERIC_FIELDS) {
      const raw = String(form[field.key] ?? "").trim();
      const value = Number(raw);
      if (raw === "" || !Number.isSafeInteger(value))
        return (settingsError = `${field.label} must be a number.`);
      if (value < field.min)
        return (settingsError = `${field.label} must be at least ${field.min}.`);
      payload[field.key] = value;
    }
    payload.model = form.model;
    payload.blockPrompt = form.blockPrompt;

    settingsSaving = true;
    try {
      const data = await putJson("/api/settings", payload);
      applySettings(data.settings);
      settingsSuccess = "Settings saved.";
    } catch (error) {
      settingsError = messageOf(error);
    } finally {
      settingsSaving = false;
    }
  }

  function resetToDefaults() {
    if (defaults) applySettings(defaults);
  }

  /* ------------------------------------------------------------------ */
  /* Chart lifecycle                                                     */
  /* ------------------------------------------------------------------ */

  $effect(() => {
    if (!chartEl) {
      if (chart) {
        chart.destroy();
        chart = null;
      }
      return;
    }

    const points = compileTimeline(stats?.timeline, stats?.now, chartRange);
    const labels = points.map((point) => point.time);
    const requests = points.map((point) => point.requests ?? 0);
    const before = points.map((point) => point.before_tokens ?? 0);
    const after = points.map((point) => point.after_tokens ?? 0);
    const isDark = dark;
    const grid = isDark ? "rgba(148,163,184,0.14)" : "rgba(100,116,139,0.18)";
    const ticks = isDark ? "#94a3b8" : "#64748b";

    if (!chart) {
      chart = new Chart(chartEl, {
        type: "line",
        data: {
          labels,
          datasets: [
            {
              label: "Requests",
              data: requests,
              borderColor: "#22d3ee",
              backgroundColor: "rgba(34,211,238,0.12)",
              borderWidth: 2,
              pointRadius: 0,
              pointHoverRadius: 4,
              tension: 0.35,
              fill: true,
              yAxisID: "y",
            },
            {
              label: "Tokens before",
              data: before,
              borderColor: "#f59e0b",
              backgroundColor: "transparent",
              borderWidth: 1.5,
              pointRadius: 0,
              pointHoverRadius: 4,
              tension: 0.35,
              yAxisID: "y1",
            },
            {
              label: "Tokens after",
              data: after,
              borderColor: "#34d399",
              backgroundColor: "transparent",
              borderWidth: 1.5,
              pointRadius: 0,
              pointHoverRadius: 4,
              tension: 0.35,
              yAxisID: "y1",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: {
              labels: { color: ticks, boxWidth: 12, boxHeight: 12, usePointStyle: true },
            },
            tooltip: {
              callbacks: {
                title: (items) =>
                  items.length
                    ? formatTime(items[0].chart.data.labels[items[0].dataIndex])
                    : "",
              },
            },
          },
          scales: {
            x: {
              grid: { color: grid },
              ticks: {
                color: ticks,
                maxTicksLimit: 8,
                maxRotation: 0,
                callback: function (value) {
                  return formatChartTick(this.getLabelForValue(value));
                },
              },
            },
            y: {
              position: "left",
              beginAtZero: true,
              grid: { color: grid },
              ticks: { color: ticks, precision: 0 },
              title: { display: true, text: "requests", color: ticks },
            },
            y1: {
              position: "right",
              beginAtZero: true,
              grid: { drawOnChartArea: false },
              ticks: { color: ticks },
              title: { display: true, text: "tokens", color: ticks },
            },
          },
        },
      });
    } else {
      chart.data.labels = labels;
      chart.data.datasets[0].data = requests;
      chart.data.datasets[1].data = before;
      chart.data.datasets[2].data = after;
      chart.options.plugins.legend.labels.color = ticks;
      chart.options.scales.x.grid.color = grid;
      chart.options.scales.x.ticks.color = ticks;
      chart.options.scales.y.grid.color = grid;
      chart.options.scales.y.ticks.color = ticks;
      chart.options.scales.y.title.color = ticks;
      chart.options.scales.y1.ticks.color = ticks;
      chart.options.scales.y1.title.color = ticks;
      chart.update("none");
    }
  });

  $effect(() => {
    return () => {
      if (chart) {
        chart.destroy();
        chart = null;
      }
    };
  });

  /* ------------------------------------------------------------------ */
  /* Derived values                                                      */
  /* ------------------------------------------------------------------ */

  let overviewMatrix = $derived.by(() => {
    const windows = stats?.windows;
    if (!windows) return [];
    const cell = (read) =>
      WINDOWS.map((column) => {
        const entry = windows[column.id];
        return entry ? read(entry) : "—";
      });
    const rate = (field, unitMs) =>
      cell((entry) => {
        // Backend span_ms measures observed coverage; a single event spans 0 ms,
        // so fall back to the nominal unit duration to keep the rate finite.
        const span = entry.span_ms > 0 ? entry.span_ms : unitMs;
        const total =
          field === "tokens"
            ? (entry.in ?? 0) + (entry.out ?? 0)
            : (entry[field] ?? 0);
        return formatRate(total / (span / unitMs));
      });
    return [
      { label: "Requests", cells: cell((entry) => formatNumber(entry.count)) },
      { label: "Req/min", cells: rate("count", 60000) },
      { label: "Req/h", cells: rate("count", 3600000) },
      { label: "Req/day", cells: rate("count", 86400000) },
      {
        label: "Tokens in",
        cells: cell((entry) => formatNumber(entry.in)),
        group: true,
      },
      { label: "Tokens out", cells: cell((entry) => formatNumber(entry.out)) },
      { label: "Tok/min", cells: rate("tokens", 60000) },
      { label: "Tok/h", cells: rate("tokens", 3600000) },
      { label: "Tok/day", cells: rate("tokens", 86400000) },
      {
        label: "Saved",
        cells: cell((entry) =>
          formatNumber(Math.max(0, (entry.in ?? 0) - (entry.out ?? 0))),
        ),
        group: true,
      },
      { label: "Errors", cells: cell((entry) => formatNumber(entry.errors)) },
      {
        label: "Avg time",
        cells: cell((entry) =>
          entry.count > 0 ? formatDuration(entry.duration_ms / entry.count) : "—",
        ),
      },
    ];
  });

  let hasTraffic = $derived((stats?.history?.requests ?? 0) > 0);
  let cacheHits = $derived(stats?.cache?.n ?? 0);
</script>

<div
  class="min-h-screen bg-slate-100 text-slate-800 transition-colors dark:bg-slate-950 dark:text-slate-100"
>
  <div class="mx-auto w-full max-w-[1500px] px-4 py-4 sm:px-6 sm:py-6">
    <!-- Header ------------------------------------------------------- -->
    <header class="mb-5 flex flex-wrap items-center justify-between gap-4">
      <div class="flex items-center gap-3">
        <span
          class="flex h-9 w-9 items-center justify-center rounded-lg bg-cyan-500/15 text-cyan-500"
        >
          <i class="fa-solid fa-bolt" aria-hidden="true"></i>
        </span>
        <div>
          <h1 class="text-lg font-semibold tracking-tight">Context Router</h1>
          <p class="flex items-center gap-2 text-xs text-slate-500 dark:text-slate-400">
            <span
              class="inline-block h-2 w-2 rounded-full {disconnected
                ? 'bg-rose-500'
                : 'bg-emerald-500'}"
              aria-hidden="true"
            ></span>
            <span>{disconnected ? "disconnected" : "connected"}</span>
            <span class="text-slate-400 dark:text-slate-600">·</span>
            <i class="fa-brands fa-python" aria-hidden="true"></i>
            <span>Python</span>
            <span class="text-slate-400 dark:text-slate-600">/</span>
            <i class="fa-solid fa-database" aria-hidden="true"></i>
            <span>SQLite</span>
          </p>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
          onclick={refreshStats}
          disabled={refreshingStats}
          aria-label="Refresh dashboard data"
        >
          <i
            class="fa-solid fa-rotate {refreshingStats ? 'fa-spin' : ''}"
            aria-hidden="true"
          ></i>
          Refresh
        </button>
        <button
          type="button"
          class="inline-flex items-center justify-center rounded-md border border-slate-300 bg-white px-2.5 py-1.5 text-slate-700 transition hover:bg-slate-50 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800"
          onclick={() => (dark = !dark)}
          aria-label={dark ? "Switch to light mode" : "Switch to dark mode"}
          title={dark ? "Switch to light mode" : "Switch to dark mode"}
        >
          <i
            class={dark ? "fa-solid fa-sun" : "fa-solid fa-moon"}
            aria-hidden="true"
          ></i>
        </button>
      </div>
    </header>

    <!-- Tabs --------------------------------------------------------- -->
    <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
    <nav
      class="mb-5 flex flex-wrap gap-1 rounded-lg border border-slate-200 bg-white p-1 dark:border-slate-800 dark:bg-slate-900"
      role="tablist"
      aria-label="Dashboard sections"
    >
      {#each TABS as entry (entry.id)}
        <button
          id={`tab-${entry.id}`}
          type="button"
          role="tab"
          aria-selected={tab === entry.id}
          aria-controls={`panel-${entry.id}`}
          tabindex={tab === entry.id ? 0 : -1}
          class="inline-flex flex-1 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-medium transition {tab === entry.id
            ? 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300'
            : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200'}"
          onclick={() => selectTab(entry.id)}
        >
          <i class={entry.icon} aria-hidden="true"></i>
          {entry.label}
        </button>
      {/each}
    </nav>

    <!-- Connection banner ------------------------------------------- -->
    {#if disconnected}
      <div
        class="mb-5 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-rose-400/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-200"
        role="alert"
      >
        <div class="flex items-center gap-2">
          <i class="fa-solid fa-plug-circle-xmark" aria-hidden="true"></i>
          <span>
            Disconnected from the context router{statsError ? `: ${statsError}` : ""}.
          </span>
        </div>
        <button
          type="button"
          class="inline-flex items-center gap-2 rounded-md border border-rose-400/50 bg-rose-500/10 px-3 py-1.5 font-medium transition hover:bg-rose-500/20"
          onclick={refreshStats}
          disabled={refreshingStats}
        >
          <i class="fa-solid fa-rotate" aria-hidden="true"></i>
          Retry
        </button>
      </div>
    {/if}

    <main>
      <!-- Overview --------------------------------------------------- -->
      <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
      <section
        id="panel-overview"
        role="tabpanel"
        aria-labelledby="tab-overview"
        hidden={tab !== "overview"}
      >
        {#if stats}
          <div
            class="scroll-area overflow-x-auto rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"
          >
            <table
              class="w-full min-w-[560px] text-left text-xs"
              aria-label="Request and token metrics by time window"
            >
              <thead
                class="border-b border-slate-200 text-slate-500 dark:border-slate-800 dark:text-slate-400"
              >
                <tr>
                  <th scope="col" class="px-3 py-2 font-medium">Metric</th>
                  {#each WINDOWS as column (column.id)}
                    <th scope="col" class="px-3 py-2 text-right font-medium">{column.label}</th>
                  {/each}
                </tr>
              </thead>
              <tbody class="tabular-nums">
                {#each overviewMatrix as row (row.label)}
                  <tr
                    class="border-b border-slate-100 last:border-0 dark:border-slate-800/60 {row.group
                      ? 'border-t border-slate-200 dark:border-slate-700'
                      : ''}"
                  >
                    <th
                      scope="row"
                      class="whitespace-nowrap px-3 py-1.5 font-medium text-slate-600 dark:text-slate-300"
                    >
                      {row.label}
                    </th>
                    {#each row.cells as value}
                      <td class="whitespace-nowrap px-3 py-1.5 text-right">{value}</td>
                    {/each}
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>

          <div
            class="mt-4 rounded-lg border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900"
          >
            <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div class="flex flex-wrap items-center gap-3">
                <h2 class="flex items-center gap-2 text-sm font-semibold">
                  <i class="fa-solid fa-chart-line text-cyan-500" aria-hidden="true"></i>
                  {chartConfig(chartRange).label}
                </h2>
                <div
                  class="flex items-center gap-1 rounded-md border border-slate-200 p-0.5 dark:border-slate-800"
                  role="group"
                  aria-label="Chart range"
                >
                  {#each RANGE_OPTIONS as option (option.id)}
                    <button
                      type="button"
                      class="rounded px-2 py-1 text-xs font-medium transition {chartRange === option.id
                        ? 'bg-cyan-500/15 text-cyan-700 dark:text-cyan-300'
                        : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-800 dark:hover:text-slate-200'}"
                      aria-pressed={chartRange === option.id}
                      onclick={() => selectChartRange(option.id)}
                    >
                      {option.label}
                    </button>
                  {/each}
                </div>
              </div>
              <p class="text-xs text-slate-500 dark:text-slate-400">
                {#if Number.isFinite(lastUpdated)}
                  Updated {formatTime(lastUpdated)}
                {:else}
                  Waiting for data…
                {/if}
              </p>
            </div>

            {#if hasTraffic}
              <div class="relative h-72">
                <canvas bind:this={chartEl} aria-label="{chartConfig(chartRange).label} request and token timeline"></canvas>
              </div>
            {:else}
              <div
                class="flex h-48 flex-col items-center justify-center gap-2 rounded-md border border-dashed border-slate-300 text-center text-sm text-slate-500 dark:border-slate-700 dark:text-slate-400"
              >
                <i class="fa-solid fa-inbox text-2xl" aria-hidden="true"></i>
                <p>No requests recorded yet. Traffic will appear here once the router handles a turn.</p>
              </div>
            {/if}
          </div>

          <p class="mt-3 text-xs text-slate-500 dark:text-slate-400">
            {formatNumber(stats.history?.compacted)} transformed requests ·
            {formatDuration(stats.history?.duration_ms)} cumulative processing ·
            {formatNumber(cacheHits)} cached blocks
          </p>
        {:else if statsError}
          <div
            class="rounded-lg border border-rose-400/40 bg-rose-500/10 px-4 py-6 text-center text-sm text-rose-700 dark:text-rose-200"
          >
            <i class="fa-solid fa-triangle-exclamation mb-2 text-xl" aria-hidden="true"></i>
            <p>Could not load dashboard statistics: {statsError}</p>
          </div>
        {:else}
          <div
            class="flex h-48 items-center justify-center gap-2 rounded-lg border border-slate-200 bg-white text-sm text-slate-500 dark:border-slate-800 dark:bg-slate-900 dark:text-slate-400"
          >
            <i class="fa-solid fa-spinner fa-spin" aria-hidden="true"></i>
            Loading dashboard…
          </div>
        {/if}
      </section>

      <!-- Requests --------------------------------------------------- -->
      <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
      <section
        id="panel-requests"
        role="tabpanel"
        aria-labelledby="tab-requests"
        hidden={tab !== "requests"}
      >
        <div
          class="mb-4 flex flex-wrap items-center gap-3 rounded-lg border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900"
        >
          <label class="relative flex-1 min-w-[200px]">
            <span class="sr-only">Search requests</span>
            <i
              class="fa-solid fa-magnifying-glass pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-400"
              aria-hidden="true"
            ></i>
            <input
              type="search"
              bind:value={search}
              oninput={onSearchInput}
              placeholder="Search requests…"
              aria-label="Search requests"
              class="w-full rounded-md border border-slate-300 bg-white py-2 pl-9 pr-3 text-sm text-slate-800 outline-none transition placeholder:text-slate-400 focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/20 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
            />
          </label>

          <label class="flex items-center gap-2 text-sm text-slate-600 dark:text-slate-300">
            <span>Changed</span>
            <select
              bind:value={changed}
              onchange={onChangedChange}
              aria-label="Filter by changed state"
              class="rounded-md border border-slate-300 bg-white px-2 py-2 text-sm text-slate-800 outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/20 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
            >
              <option value="all">All</option>
              <option value="yes">Yes</option>
              <option value="no">No</option>
            </select>
          </label>

          <button
            type="button"
            class="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:opacity-50 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200 dark:hover:bg-slate-800"
            onclick={loadHistory}
            disabled={historyLoading}
            aria-label="Reload requests"
          >
            <i class="fa-solid fa-rotate {historyLoading ? 'fa-spin' : ''}" aria-hidden="true"></i>
            Reload
          </button>

          <span class="ml-auto text-xs text-slate-500 dark:text-slate-400">
            {formatNumber(history.length)} of {formatNumber(total)} shown
          </span>
        </div>

        {#if historyError}
          <div
            class="mb-4 rounded-lg border border-rose-400/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-700 dark:text-rose-200"
            role="alert"
          >
            Failed to load requests: {historyError}
          </div>
        {/if}

        <div
          class="overflow-hidden rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"
        >
          <div class="scroll-area overflow-x-auto">
            <table class="w-full min-w-[720px] text-left text-sm">
              <thead
                class="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500 dark:border-slate-800 dark:text-slate-400"
              >
                <tr>
                  <th scope="col" class="px-3 py-2 font-medium">Status</th>
                  <th scope="col" class="px-3 py-2 font-medium">Model</th>
                  <th scope="col" class="px-3 py-2 font-medium">Created</th>
                  <th scope="col" class="px-3 py-2 font-medium">Duration</th>
                  <th scope="col" class="px-3 py-2 text-right font-medium">Tokens</th>
                  <th scope="col" class="px-3 py-2 font-medium">Cached</th>
                  <th scope="col" class="px-3 py-2 font-medium">
                    <span class="sr-only">Detail</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {#if history.length === 0 && !historyLoading}
                  <tr>
                    <td colspan="7" class="px-3 py-10 text-center text-slate-500 dark:text-slate-400">
                      <i class="fa-solid fa-inbox mb-2 block text-2xl" aria-hidden="true"></i>
                      No requests match this view yet.
                    </td>
                  </tr>
                {/if}
                {#each history as item (item.id)}
                  {@const badge = statusBadge(item.status)}
                  {@const selected = detailId === item.id}
                  <tr
                    class="border-b border-slate-100 transition last:border-0 dark:border-slate-800/60 {selected
                      ? 'bg-cyan-500/5'
                      : 'hover:bg-slate-50 dark:hover:bg-slate-800/50'}"
                  >
                    <td class="px-3 py-2">
                      <span
                        class="inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium {badge.cls}"
                      >
                        <i class={badge.icon} aria-hidden="true"></i>
                        {badge.label}
                      </span>
                    </td>
                    <td class="px-3 py-2 font-mono text-xs">{item.model || "—"}</td>
                    <td class="px-3 py-2 whitespace-nowrap text-xs">{formatTime(item.created_at)}</td>
                    <td class="px-3 py-2 whitespace-nowrap text-xs">{formatDuration(item.duration_ms)}</td>
                    <td class="px-3 py-2 whitespace-nowrap text-right tabular-nums">{formatTokenFlow(item.before_tokens, item.after_tokens)}</td>
                    <td class="px-3 py-2">
                      {#if item.cache_hits > 0}
                        <span
                          class="inline-flex items-center gap-1 text-xs text-violet-600 dark:text-violet-300"
                        >
                          <i class="fa-solid fa-bolt" aria-hidden="true"></i>
                          {item.cache_hits}
                        </span>
                      {:else}
                        <span class="text-xs text-slate-400">—</span>
                      {/if}
                    </td>
                    <td class="px-3 py-2 text-right">
                      <button
                        type="button"
                        class="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                        onclick={() => toggleRow(item.id)}
                        aria-expanded={selected}
                        aria-label={selected ? `Collapse request ${item.id}` : `Expand request ${item.id}`}
                      >
                        <i
                          class="fa-solid {selected ? 'fa-chevron-up' : 'fa-chevron-down'}"
                          aria-hidden="true"
                        ></i>
                        {selected ? "Hide" : "View"}
                      </button>
                    </td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>

          <div
            class="flex items-center justify-between gap-3 border-t border-slate-200 px-3 py-2 dark:border-slate-800"
          >
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200 dark:hover:bg-slate-800"
              onclick={newest}
              disabled={beforeCursor == null || historyLoading}
            >
              <i class="fa-solid fa-angles-left" aria-hidden="true"></i>
              Newest
            </button>
            <span class="text-xs text-slate-500 dark:text-slate-400">Page size 50</span>
            <button
              type="button"
              class="inline-flex items-center gap-2 rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 transition hover:bg-slate-50 disabled:cursor-not-allowed disabled:opacity-40 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-200 dark:hover:bg-slate-800"
              onclick={older}
              disabled={nextCursor == null || historyLoading}
            >
              Older
              <i class="fa-solid fa-angles-right" aria-hidden="true"></i>
            </button>
          </div>
        </div>

        <!-- Detail ------------------------------------------------- -->
        {#if detailId != null}
          <div
            class="mt-4 rounded-lg border border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"
          >
            <div
              class="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 dark:border-slate-800"
            >
              <h2 class="flex items-center gap-2 text-sm font-semibold">
                <i class="fa-solid fa-code text-cyan-500" aria-hidden="true"></i>
                Request #{detailId}
              </h2>
              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
                onclick={() => toggleRow(detailId)}
              >
                <i class="fa-solid fa-xmark" aria-hidden="true"></i>
                Close
              </button>
            </div>

            {#if detailLoading}
              <p class="px-4 py-6 text-sm text-slate-500 dark:text-slate-400">
                <i class="fa-solid fa-spinner fa-spin mr-2" aria-hidden="true"></i>
                Loading detail…
              </p>
            {:else if detailError}
              <p class="px-4 py-6 text-sm text-rose-600 dark:text-rose-300" role="alert">
                {detailError}
              </p>
            {:else if detail}
              <div class="grid grid-cols-2 gap-x-6 gap-y-1 px-4 py-3 text-xs sm:grid-cols-4">
                <div>
                  <p class="text-slate-500 dark:text-slate-400">Created</p>
                  <p>{formatTime(detail.created_at)}</p>
                </div>
                <div>
                  <p class="text-slate-500 dark:text-slate-400">Model</p>
                  <p class="font-mono">{detail.model || "—"}</p>
                </div>
                <div>
                  <p class="text-slate-500 dark:text-slate-400">Duration</p>
                  <p>{formatDuration(detail.duration_ms)}</p>
                </div>
                <div class="col-span-2">
                  <p class="text-slate-500 dark:text-slate-400">Tokens</p>
                  <p class="tabular-nums">{formatTokenFlow(detail.before_tokens, detail.after_tokens)}</p>
                </div>
                <div>
                  <p class="text-slate-500 dark:text-slate-400">Changed</p>
                  <p>{detail.changed ? "yes" : "no"}</p>
                </div>
                <div>
                  <p class="text-slate-500 dark:text-slate-400">Cache hits</p>
                  <p class="tabular-nums">{formatNumber(detail.cache_hits)}</p>
                </div>
                {#if detail.error}
                  <div class="col-span-full">
                    <p class="text-slate-500 dark:text-slate-400">Error</p>
                    <p class="text-rose-600 dark:text-rose-300">{detail.error}</p>
                  </div>
                {/if}
              </div>

              {#if notice}
                <p class="px-4 pb-2 text-xs text-cyan-600 dark:text-cyan-300">{notice}</p>
              {/if}

              <div class="grid grid-cols-1 gap-4 px-4 pb-4 xl:grid-cols-2">
                {@render JsonPane({
                  title: "Original",
                  icon: "fa-solid fa-file-import",
                  filename: `request-${detail.id}-original.json`,
                  text: pretty(detail.original),
                  oncopy: () => (notice = "Original JSON copied."),
                })}
                {@render JsonPane({
                  title: "Forwarded",
                  icon: "fa-solid fa-file-export",
                  filename: `request-${detail.id}-forwarded.json`,
                  text: pretty(detail.compressed),
                  oncopy: () => (notice = "Forwarded JSON copied."),
                })}
                <div class="xl:col-span-2">
                  {@render JsonPane({
                    title: "Changes",
                    icon: "fa-solid fa-list",
                    filename: `request-${detail.id}-changes.json`,
                    text: pretty(detail.changes),
                    oncopy: () => (notice = "Changes JSON copied."),
                  })}
                </div>
              </div>
            {/if}
          </div>
        {/if}
      </section>

      <!-- Settings --------------------------------------------------- -->
      <!-- svelte-ignore a11y_no_noninteractive_element_to_interactive_role -->
      <section
        id="panel-settings"
        role="tabpanel"
        aria-labelledby="tab-settings"
        hidden={tab !== "settings"}
      >
        <div
          class="max-w-3xl rounded-lg border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900"
        >
          <h2 class="flex items-center gap-2 text-sm font-semibold">
            <i class="fa-solid fa-sliders text-cyan-500" aria-hidden="true"></i>
            Condensation settings
          </h2>

          {#if settingsLoading}
            <p class="mt-4 text-sm text-slate-500 dark:text-slate-400">
              <i class="fa-solid fa-spinner fa-spin mr-2" aria-hidden="true"></i>
              Loading settings…
            </p>
          {:else}
            {#if settingsError}
              <p
                class="mt-4 rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-sm text-rose-700 dark:text-rose-200"
                role="alert"
              >
                {settingsError}
              </p>
            {/if}
            {#if settingsSuccess}
              <p
                class="mt-4 rounded-md border border-emerald-400/40 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-700 dark:text-emerald-200"
                role="status"
              >
                <i class="fa-solid fa-circle-check mr-2" aria-hidden="true"></i>
                {settingsSuccess}
              </p>
            {/if}

            <div class="mt-5 grid grid-cols-1 gap-4 sm:grid-cols-2">
              {#each NUMERIC_FIELDS as field (field.key)}
                <label class="block">
                  <span class="text-sm font-medium">{field.label}</span>
                  <input
                    type="number"
                    min={field.min}
                    step={field.step}
                    bind:value={form[field.key]}
                    aria-label={field.label}
                    class="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/20 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                  />
                  <span class="mt-1 block text-xs text-slate-500 dark:text-slate-400">
                    {field.hint} Unit: {field.unit}.
                  </span>
                </label>
              {/each}

              <label class="block">
                <span class="text-sm font-medium">Condensation model</span>
                <select
                  bind:value={form.model}
                  aria-label="Condensation model"
                  class="mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/20 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
                >
                  {#each MODEL_OPTIONS as option (option.value)}
                    <option value={option.value}>{option.label}</option>
                  {/each}
                </select>
              </label>
            </div>

            <label class="mt-4 block">
              <span class="text-sm font-medium">Block prompt</span>
              <textarea
                rows="6"
                bind:value={form.blockPrompt}
                aria-label="Block prompt"
                class="scroll-area mt-1 w-full resize-y rounded-md border border-slate-300 bg-white px-3 py-2 font-mono text-xs text-slate-800 outline-none transition focus:border-cyan-500 focus:ring-2 focus:ring-cyan-500/20 dark:border-slate-700 dark:bg-slate-950 dark:text-slate-100"
              ></textarea>
            </label>

            <div class="mt-5 flex flex-wrap items-center gap-3">
              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-md bg-cyan-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-cyan-500 disabled:opacity-50"
                onclick={saveSettings}
                disabled={settingsSaving}
              >
                <i class="fa-solid fa-floppy-disk" aria-hidden="true"></i>
                {settingsSaving ? "Saving…" : "Apply settings"}
              </button>
              <button
                type="button"
                class="inline-flex items-center gap-2 rounded-md border border-slate-300 px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800"
                onclick={resetToDefaults}
              >
                <i class="fa-solid fa-rotate-left" aria-hidden="true"></i>
                Reset to defaults
              </button>
            </div>
          {/if}
        </div>
      </section>
    </main>
  </div>
</div>

<!-- JSON pane snippet ------------------------------------------------ -->
{#snippet JsonPane(props)}
  <div>
    <div class="mb-1 flex items-center justify-between gap-2">
      <h3 class="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-500 dark:text-slate-400">
        <i class={props.icon} aria-hidden="true"></i>
        {props.title}
      </h3>
      <div class="flex items-center gap-1">
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
          onclick={async () => {
            if (await copyText(props.text)) props.oncopy?.();
          }}
          aria-label={`Copy ${props.title} JSON`}
        >
          <i class="fa-solid fa-copy" aria-hidden="true"></i>
          Copy
        </button>
        <button
          type="button"
          class="inline-flex items-center gap-1 rounded-md border border-slate-300 px-2 py-1 text-xs font-medium text-slate-600 transition hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
          onclick={() => downloadText(props.filename, props.text)}
          aria-label={`Download ${props.title} JSON`}
        >
          <i class="fa-solid fa-download" aria-hidden="true"></i>
          Download
        </button>
      </div>
    </div>
    <pre
      class="scroll-area max-h-96 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 font-mono text-xs leading-relaxed text-slate-700 dark:border-slate-800 dark:bg-slate-950 dark:text-slate-200">{props.text}</pre>
  </div>
{/snippet}
