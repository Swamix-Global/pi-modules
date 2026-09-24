import { Fragment, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table";
import type { RecentRequest, SeriesPoint } from "./api";
import { fmt, kfmt, secondsFromMs } from "./format";

declare module "@tanstack/react-table" {
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  interface ColumnMeta<TData extends RowData, TValue> {
    align?: "left" | "right";
  }
}
import type { RowData } from "@tanstack/react-table";

/* ------------------------------- tabs ------------------------------- */

export interface TabDef {
  id: string;
  label: string;
}

export function Tabs({ tabs, active, onChange }: { tabs: TabDef[]; active: string; onChange: (id: string) => void }) {
  const refs = useRef<(HTMLButtonElement | null)[]>([]);

  function onKeyDown(event: React.KeyboardEvent, index: number) {
    const last = tabs.length - 1;
    let next = index;
    if (event.key === "ArrowRight") next = index === last ? 0 : index + 1;
    else if (event.key === "ArrowLeft") next = index === 0 ? last : index - 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = last;
    else return;
    event.preventDefault();
    onChange(tabs[next].id);
    refs.current[next]?.focus();
  }

  return (
    <div
      role="tablist"
      aria-label="Dashboard views"
      className="inline-flex overflow-hidden border border-line bg-panel"
    >
      {tabs.map((tab, index) => {
        const selected = tab.id === active;
        return (
          <button
            key={tab.id}
            ref={(el) => {
              refs.current[index] = el;
            }}
            role="tab"
            id={`tab-${tab.id}`}
            aria-selected={selected}
            aria-controls={`panel-${tab.id}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(tab.id)}
            onKeyDown={(event) => onKeyDown(event, index)}
            className={`border-l border-line px-3.5 py-2 text-[12.5px] first:border-l-0 ${
              selected ? "bg-accentbg font-semibold text-accent" : "text-muted hover:text-body"
            }`}
          >
            {tab.label}
          </button>
        );
      })}
    </div>
  );
}

/* ------------------------------ panels ------------------------------ */

export function Panel({
  title,
  actions,
  children,
  className = "",
  bodyClassName = "",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`min-w-0 border border-line bg-ink ${className}`}>
      {(title || actions) && (
        <h2 className="flex items-center justify-between gap-2 border-b border-line bg-panel2 px-2.5 py-1.5 text-[10.5px] font-bold uppercase tracking-[0.1em] text-muted">
          <span className="min-w-0 truncate">{title}</span>
          {actions}
        </h2>
      )}
      <div className={bodyClassName}>{children}</div>
    </section>
  );
}

export function Metrics({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="m-0">
      {rows.map(([label, value], index) => (
        <div
          key={`${label}-${index}`}
          className={`flex items-baseline justify-between gap-3 border-b border-line px-2.5 py-1 last:border-b-0 ${
            index % 2 ? "bg-panel" : ""
          }`}
        >
          <dt className="min-w-0 break-words text-muted">{label}</dt>
          <dd className="min-w-0 break-words text-right">{value}</dd>
        </div>
      ))}
    </dl>
  );
}

export function StatCard({
  label,
  value,
  detail,
  tone = "body",
}: {
  label: string;
  value: ReactNode;
  detail?: ReactNode;
  tone?: "body" | "accent" | "pos" | "neg";
}) {
  const toneClass =
    tone === "accent" ? "text-accent" : tone === "pos" ? "text-pos" : tone === "neg" ? "text-neg" : "text-body";
  return (
    <div className="min-w-0 border border-line bg-panel px-3 py-2">
      <div className="text-[10.5px] font-bold uppercase tracking-[0.1em] text-muted">{label}</div>
      <div className={`mt-0.5 text-[22px] font-bold leading-tight ${toneClass}`}>{value}</div>
      {detail && <div className="mt-0.5 text-[12px] text-muted">{detail}</div>}
    </div>
  );
}

export function Distribution({ title, map }: { title: string; map: Record<string, number> | undefined }) {
  const entries = Object.entries(map ?? {}).sort((a, b) => b[1] - a[1]);
  return (
    <div className="min-w-0">
      <div className="mb-1 text-[10.5px] font-bold uppercase tracking-[0.1em] text-muted">{title}</div>
      <div className="flex min-h-[22px] flex-wrap gap-1">
        {entries.length === 0 && <span className="text-muted">—</span>}
        {entries.map(([key, count]) => (
          <span key={key} className="border border-line bg-panel px-1.5 py-0.5 text-[12px]" title={`${key}: ${fmt(count)}`}>
            {key} <span className="text-muted">{fmt(count)}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

/* --------------------------- segmented --------------------------- */

export function Segmented<T extends string>({
  label,
  value,
  options,
  onChange,
  disabled = false,
}: {
  label?: string;
  value: T;
  options: { value: T; label: string; title?: string }[];
  onChange: (value: T) => void;
  disabled?: boolean;
}) {
  return (
    <div className="inline-flex items-center overflow-hidden border border-line bg-panel" role="group" aria-label={label}>
      {label && <span className="px-2.5 py-2 text-[12.5px] text-muted">{label}</span>}
      {options.map((option) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            type="button"
            aria-pressed={selected}
            disabled={disabled}
            title={option.title}
            onClick={() => !selected && onChange(option.value)}
            className={`border-l border-line px-3 py-2 text-[12.5px] first:border-l-0 disabled:cursor-not-allowed disabled:opacity-50 ${
              selected ? "bg-accentbg font-semibold text-accent" : "text-muted hover:text-body"
            }`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}

/* ---------------------------- settings sliders ---------------------------- */

export const BUDGET_POINTS = [50_000, 64_000, 100_000, 128_000, 200_000, 256_000];
export const OUTPUT_POINTS = [50, 100, 200, 400];

export function SettingSlider({ label, points, value, onCommit, disabled = false }: {
  label: string;
  points: readonly number[];
  value: number;
  onCommit: (tokens: number) => void;
  disabled?: boolean;
}) {
  const nearest = (tokens: number) => points.reduce((best, point, i) =>
    Math.abs(point - tokens) < Math.abs(points[best] - tokens) ? i : best, 0);
  const [index, setIndex] = useState(() => nearest(value));
  useEffect(() => setIndex(nearest(value)), [value]);
  const labelId = useId();

  return (
    <div className="flex items-center gap-2 border border-line bg-panel px-2.5 py-1.5">
      <span id={labelId} className="text-[12.5px] text-muted">{label}</span>
      <div className="w-[190px]">
        <input
          className="ctx-range"
          type="range"
          min={0}
          max={points.length - 1}
          step={1}
          value={index}
          disabled={disabled}
          aria-labelledby={labelId}
          aria-valuetext={`${kfmt(points[index])} tokens`}
          onChange={(event) => {
            const next = Number(event.target.value);
            setIndex(next);
            onCommit(points[next]);
          }}
        />
        <div className="flex justify-between text-[10.5px] text-muted">
          {points.map((point, i) => (
            <span key={point} className={i === index ? "font-bold text-accent" : ""}>{kfmt(point)}</span>
          ))}
        </div>
      </div>
      <output className="min-w-[3.6em] text-right text-[12.5px] font-semibold text-accent">
        {kfmt(points[index])}
      </output>
    </div>
  );
}

/* ---------------------------- data table ---------------------------- */

interface DataTableProps<T> {
  columns: ColumnDef<T, any>[];
  data: T[];
  empty?: ReactNode;
  initialSorting?: SortingState;
  onRowClick?: (row: T) => void;
  rowAriaLabel?: (row: T) => string;
  subRow?: (row: T) => ReactNode;
  minWidth?: number;
}

export function DataTable<T>({
  columns,
  data,
  empty = "no rows",
  initialSorting = [],
  onRowClick,
  rowAriaLabel,
  subRow,
  minWidth = 840,
}: DataTableProps<T>) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting);
  const table = useReactTable({
    data,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  return (
    <div className="max-h-[72vh] overflow-auto">
      <table className="w-full border-collapse text-[12.5px]" style={{ minWidth }}>
        <thead>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => {
                const canSort = header.column.getCanSort();
                const sorted = header.column.getIsSorted();
                const align = header.column.columnDef.meta?.align === "right" ? "text-right" : "text-left";
                return (
                  <th
                    key={header.id}
                    scope="col"
                    style={{ width: header.getSize() }}
                    className={`sticky top-0 z-10 border-b border-line bg-panel2 px-1.5 py-1 text-[10.5px] font-bold uppercase tracking-[0.08em] text-muted ${align}`}
                  >
                    {header.isPlaceholder ? null : canSort ? (
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className={`inline-flex w-full items-center gap-1 ${align === "text-right" ? "justify-end" : ""} hover:text-body`}
                        title={`sort by ${String(header.column.columnDef.header ?? header.id)}`}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        <span aria-hidden="true">{sorted === "asc" ? "▲" : sorted === "desc" ? "▼" : ""}</span>
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {table.getRowModel().rows.length === 0 && (
            <tr>
              <td colSpan={columns.length} className="px-2 py-3 text-muted">
                {empty}
              </td>
            </tr>
          )}
          {table.getRowModel().rows.map((row) => (
            <Fragment key={row.id}>
              <tr
                onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                aria-label={rowAriaLabel?.(row.original)}
                className={`border-b border-line ${onRowClick ? "cursor-pointer hover:bg-panel" : ""}`}
              >
                {row.getVisibleCells().map((cell) => {
                  const align = cell.column.columnDef.meta?.align === "right" ? "text-right" : "text-left";
                  return (
                    <td key={cell.id} className={`max-w-0 overflow-hidden px-1.5 py-1 text-ellipsis whitespace-nowrap ${align}`}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  );
                })}
              </tr>
              {subRow && subRow(row.original) != null && (
                <tr className="border-b border-line bg-panel">
                  <td colSpan={row.getVisibleCells().length} className="px-3 py-2">
                    {subRow(row.original)}
                  </td>
                </tr>
              )}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ------------------------------ charts ------------------------------ */

const CHART_W = 1000;
const CHART_H = 150;

export function ReclaimedChart({ series, now }: { series: SeriesPoint[]; now: number }) {
  const points = useMemo(() => series ?? [], [series]);
  if (points.length === 0) {
    return <p className="px-3 py-4 text-muted">no requests in the last 24 h</p>;
  }
  const end = now;
  const start = end - 24 * 3600;
  const base = CHART_H - 20;
  const top = 12;
  const max = Math.max(1, ...points.map((point) => point.saved_tokens));
  const totalSaved = points.reduce((sum, point) => sum + point.saved_tokens, 0);
  const totalReqs = points.reduce((sum, point) => sum + point.count, 0);
  const barWidth = Math.max(1.5, (CHART_W / (24 * 60)) * 0.8);

  const bars = points
    .filter((point) => point.saved_tokens > 0)
    .map((point) => {
      const height = Math.max(1, (point.saved_tokens / max) * (base - top));
      const x = Math.min(CHART_W - barWidth, Math.max(0, ((point.minute * 60 - start) / (end - start)) * CHART_W));
      const time = new Date(point.minute * 60_000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      return (
        <rect
          key={point.minute}
          x={x.toFixed(1)}
          y={(base - height).toFixed(1)}
          width={barWidth.toFixed(2)}
          height={height.toFixed(1)}
          fill="var(--color-accent)"
          opacity={0.72}
        >
          <title>{`${time} · ${fmt(point.saved_tokens)} tokens reclaimed · ${point.count} req`}</title>
        </rect>
      );
    });

  const grid = [0, 0.25, 0.5, 0.75, 1].map((fraction) => {
    const y = (base - fraction * (base - top)).toFixed(1);
    return <line key={fraction} x1={0} y1={y} x2={CHART_W} y2={y} stroke="var(--color-line)" />;
  });

  const labels = [0, 6, 12, 18, 24].map((hour) => {
    const x = (hour / 24) * CHART_W;
    const time = new Date((start + hour * 3600) * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    return (
      <text
        key={hour}
        x={x.toFixed(1)}
        y={CHART_H - 4}
        fontSize={15}
        fill="var(--color-muted)"
        textAnchor={hour === 0 ? "start" : hour === 24 ? "end" : "middle"}
      >
        {time}
      </text>
    );
  });

  return (
    <div>
      <div className="flex flex-wrap justify-between gap-2 px-3 pb-1 pt-2 text-[12px] text-muted">
        <span>{fmt(totalSaved)} tokens reclaimed over {fmt(totalReqs)} requests</span>
        <span>peak {fmt(max)} / min</span>
      </div>
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        role="img"
        aria-label={`Context reclaimed per minute over the last 24 hours: ${fmt(totalSaved)} tokens over ${fmt(totalReqs)} requests`}
        className="block h-auto w-full"
      >
        {grid}
        {bars}
        {labels}
      </svg>
    </div>
  );
}

export function ThroughputChart({ recent }: { recent: RecentRequest[] }) {
  const rows = useMemo(() => [...(recent ?? [])].sort((a, b) => a.at - b.at), [recent]);
  if (rows.length < 2) {
    return <p className="px-3 py-4 text-muted">not enough requests in the window for a throughput trend</p>;
  }
  const base = CHART_H - 20;
  const top = 12;
  const first = rows[0].at;
  const last = rows[rows.length - 1].at;
  const span = Math.max(1, last - first);

  const samples = rows.map((row, index) => {
    let startIndex = index;
    while (startIndex > 0 && rows[startIndex - 1].at >= row.at - 60) startIndex -= 1;
    const count = index - startIndex + 1;
    const localSpan = Math.max(1, row.at - rows[startIndex].at);
    return { at: row.at, rpm: (count / localSpan) * 60, ms: row.ms ?? 0 };
  });

  const maxRpm = Math.max(1, ...samples.map((sample) => sample.rpm));
  const maxMs = Math.max(1, ...samples.map((sample) => sample.ms));
  const x = (at: number) => ((at - first) / span) * CHART_W;
  const yRpm = (value: number) => base - (value / maxRpm) * (base - top);
  const yMs = (value: number) => base - (value / maxMs) * (base - top);

  const area = `M ${x(samples[0].at).toFixed(1)} ${base} ${samples
    .map((sample) => `L ${x(sample.at).toFixed(1)} ${yRpm(sample.rpm).toFixed(1)}`)
    .join(" ")} L ${x(samples[samples.length - 1].at).toFixed(1)} ${base} Z`;
  const line = samples.map((sample) => `${x(sample.at).toFixed(1)},${yRpm(sample.rpm).toFixed(1)}`).join(" ");
  const msLine = samples.map((sample) => `${x(sample.at).toFixed(1)},${yMs(sample.ms).toFixed(1)}`).join(" ");

  return (
    <div>
      <div className="flex flex-wrap gap-4 px-3 pb-1 pt-2 text-[12px] text-muted">
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-accent" />
          throughput {maxRpm.toFixed(1)} req/min peak
        </span>
        <span>
          <span className="mr-1 inline-block h-2 w-2 rounded-full bg-pos" />
          latency {secondsFromMs(maxMs)} peak
        </span>
      </div>
      <svg
        viewBox={`0 0 ${CHART_W} ${CHART_H}`}
        role="img"
        aria-label={`Rolling throughput and latency across the last ${rows.length} requests`}
        className="block h-auto w-full"
      >
        {[0, 0.25, 0.5, 0.75, 1].map((fraction) => {
          const y = (base - fraction * (base - top)).toFixed(1);
          return <line key={fraction} x1={0} y1={y} x2={CHART_W} y2={y} stroke="var(--color-line)" />;
        })}
        <path d={area} fill="var(--color-accent)" opacity={0.18} />
        <polyline points={line} fill="none" stroke="var(--color-accent)" strokeWidth={2} />
        <polyline points={msLine} fill="none" stroke="var(--color-pos)" strokeWidth={1.5} strokeDasharray="4 3" />
      </svg>
    </div>
  );
}
