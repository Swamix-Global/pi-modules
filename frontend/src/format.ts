// Display formatting at the edge: every computed float is rounded here before it reaches the DOM,
// and all relative times use the server clock published by /stats so host/browser skew cannot shift
// the ages or the 24 h chart window.

export const clock = { skew: 0 };

export function syncClock(serverNow: number): void {
  if (Number.isFinite(serverNow)) clock.skew = serverNow - Date.now() / 1000;
}

export function serverNow(): number {
  return Date.now() / 1000 + clock.skew;
}

export function r1(n: number): number {
  return Math.round(Number(n) * 10) / 10;
}

export function fmt(n: number | string | null | undefined): string {
  if (n === null || n === undefined || n === "") return "—";
  const value = Number(n);
  return Number.isFinite(value) ? value.toLocaleString() : String(n);
}

export function secondsFromMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${(value / 1000).toFixed(2)} s`;
}

export function dur(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h ? `${h} h ` : ""}${m} min ${r1(s % 60)} s`;
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value < 0 ? "−$" : "+$";
  return `${sign}${Math.abs(value).toFixed(4)}`;
}

export function usd0(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `$${Number(value).toFixed(4)}`;
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return `${Number(value).toFixed(digits)} %`;
}

export function millionTokens(n: number): string {
  return `${(n / 1_000_000).toFixed(3)} M tokens`;
}

export function thousandTokens(n: number): string {
  return `${(n / 1000).toLocaleString(undefined, { maximumFractionDigits: 3 })}k`;
}

export function kfmt(n: number): string {
  return n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

export function ago(at: number): string {
  const s = Math.max(0, serverNow() - at);
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.round(s / 60)} m`;
  return `${(s / 3600).toFixed(1)} h`;
}

export function clockTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleTimeString();
}

export function dateTime(epochSeconds: number | null | undefined): string {
  if (!epochSeconds) return "—";
  return new Date(epochSeconds * 1000).toLocaleString();
}

export function distribution(map: Record<string, number> | undefined): [string, number][] {
  return Object.entries(map ?? {}).sort((a, b) => b[1] - a[1]);
}
