import { useEffect, useRef, useState } from "react";
import { ApiError, type RequestDetail } from "./api";
import { dateTime, fmt, secondsFromMs, usd, usd0 } from "./format";
import { Metrics, Panel } from "./components";

function costSummary(detail: RequestDetail): [string, string][] {
  const cost = detail.cost;
  const rows: [string, string][] = [];
  if (!cost) {
    rows.push(["Upstream cost", "no usage captured for this request"]);
    return rows;
  }
  const billed =
    cost.sent_usd == null
      ? cost.in == null
        ? "—"
        : `${fmt(cost.in)} in · ${fmt(cost.cached)} cached · ${fmt(cost.out)} out (no declared rate)`
      : `${usd0(cost.sent_usd)}${cost.estimated ? " est" : " billed"}${
          cost.sent_usd != null && cost.in != null ? ` · ${fmt(cost.in)} in · ${fmt(cost.cached)} cached · ${fmt(cost.out)} out` : ""
        }`;
  rows.push(["Upstream cost", billed]);
  if (cost.raw_usd != null) rows.push(["Raw prompt (no compaction)", `${usd0(cost.raw_usd)} est`]);
  if (cost.condenser_usd) {
    rows.push([
      "Condenser cost",
      `${usd0(cost.condenser_usd)}${cost.condenser_calls ? ` · ${fmt(cost.condenser_calls)} call(s) · ${fmt(cost.condenser_in)} in / ${fmt(cost.condenser_out)} out` : ""}`,
    ]);
  }
  if (cost.net_usd != null) rows.push(["Net saved", `${usd(cost.net_usd)} (${fmt(cost.net_percent)} % of raw)`]);
  return rows;
}

function BlockDiff({ detail }: { detail: RequestDetail }) {
  const blocks = detail.plan?.blocks ?? [];
  if (blocks.length === 0) {
    return <p className="px-2 py-3 text-muted">no compaction plan captured for this request</p>;
  }
  return (
    <div>
      {blocks.map((block) => (
        <div key={block.id} className="border-t border-line py-1.5 text-[11.5px]">
          <div className={`font-bold ${block.dropped ? "text-neg" : "text-pos"}`}>
            − [block {block.id}] {fmt(block.tokens ?? Math.round((block.chars ?? 0) / 3.5))} tokens →{" "}
            {block.pending
              ? "history held back, note pending in the cache"
              : block.dropped
                ? "dropped, no summary kept"
                : `summary ${fmt(block.summary_tokens ?? Math.round((block.summary_chars ?? 0) / 3.5))} tokens`}
          </div>
          {block.preview && <div className="whitespace-pre-wrap break-words pl-2.5 text-neg">− {block.preview}</div>}
          {block.summary && <div className="whitespace-pre-wrap break-words pl-2.5 text-pos">+ {block.summary}</div>}
        </div>
      ))}
    </div>
  );
}

export function DiffModal({
  detail,
  error,
  loading,
  onClose,
}: {
  detail: RequestDetail | undefined;
  error: unknown;
  loading: boolean;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const [fullView, setFullView] = useState(false);

  useEffect(() => {
    closeRef.current?.focus();
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const saved =
    detail && detail.raw_tokens ? Math.round(100 * (1 - detail.sent_tokens / detail.raw_tokens)) : 0;
  const blocks = detail?.plan?.blocks ?? [];
  const pending = blocks.filter((block) => block.pending).length;

  return (
    <div
      className={`fixed inset-0 z-30 flex items-center justify-center bg-black/75 ${fullView ? "p-0" : "p-3"}`}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Compaction diff"
        className={`ctx-fade flex max-h-[84vh] w-full flex-col border border-line bg-ink ${fullView ? "h-full max-h-full max-w-full" : "max-w-[1000px]"}`}
      >
        <div className="flex items-center justify-between gap-2 border-b border-line bg-panel2 px-2.5 py-1.5">
          <strong className="text-[11px] uppercase tracking-[0.08em] text-muted">Compaction diff</strong>
          <div className="flex items-center gap-2">
            <button
              type="button"
              aria-pressed={fullView}
              onClick={() => setFullView((value) => !value)}
              className="border border-line px-2 py-0.5 text-[12.5px] text-muted hover:text-body"
            >
              {fullView ? "Exit full view" : "Full view"}
            </button>
            <button
              ref={closeRef}
              type="button"
              onClick={onClose}
              className="border border-line px-2 py-0.5 text-[12.5px] text-muted hover:text-body"
            >
              Close
            </button>
          </div>
        </div>
        <div className="overflow-auto px-3 py-2">
          {loading && <p className="py-3 text-muted">loading request…</p>}
          {!loading && error != null && (
            <p className="py-3 text-muted">
              {error instanceof ApiError && error.status === 404
                ? "this request could not be loaded — the row was pruned or the router restarted since."
                : "the compaction diff could not be loaded."}
            </p>
          )}
          {!loading && !error && detail && (
            <>
              <Metrics
                rows={[
                  ["Request", `#${detail.id} · ${dateTime(detail.at)}`],
                  ["Route / model", `${detail.route} · ${detail.model || "—"}`],
                  [
                    "Result",
                    `${detail.result} (HTTP ${detail.status == null ? "—" : detail.status}, ${secondsFromMs(detail.ms)})`,
                  ],
                  [
                    "Tokens raw → sent",
                    `${fmt(detail.raw_tokens)} → ${fmt(detail.sent_tokens)} (${saved} % saved)`,
                  ],
                  ...costSummary(detail),
                  [
                    "Blocks removed",
                    `${blocks.length} blocks, ${detail.plan?.kept ?? 0} summarized${pending ? `, ${pending} pending` : ""}${
                      detail.plan?.omitted ? `, ${detail.plan.omitted} omitted` : ""
                    }`,
                  ],
                ]}
              />
              <div className="mt-2 border border-line">
                <Panel title="Block-level diff (stored excerpts)">
                  <BlockDiff detail={detail} />
                </Panel>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
