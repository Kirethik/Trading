/**
 * Number and date formatters for the trading UI.
 * All use tabular-nums layout-safe representations.
 */

const pct = new Intl.NumberFormat("en-US", {
  style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1,
});
const pct2 = new Intl.NumberFormat("en-US", {
  style: "percent", minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const usd = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 0, maximumFractionDigits: 0,
});
const usd2 = new Intl.NumberFormat("en-US", {
  style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2,
});
const num2 = new Intl.NumberFormat("en-US", {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});

export const fmt = {
  pct:   (v: number | null | undefined) => v == null ? "—" : pct.format(v),
  pct2:  (v: number | null | undefined) => v == null ? "—" : pct2.format(v),
  usd:   (v: number | null | undefined) => v == null ? "—" : usd.format(v),
  usd2:  (v: number | null | undefined) => v == null ? "—" : usd2.format(v),
  ratio: (v: number | null | undefined, decimals = 2) =>
    v == null ? "—" : Number.isFinite(v) ? v.toFixed(decimals) : "∞",
  sign:  (v: number | null | undefined) =>
    v == null ? "—" : (v >= 0 ? "+" : "") + pct.format(v),

  date: (tsMs: number) =>
    new Date(tsMs).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }),

  shortDate: (tsMs: number) =>
    new Date(tsMs).toLocaleDateString("en-US", { month: "short", year: "2-digit" }),
};

export function dirColor(v: number | null | undefined): string {
  if (v == null) return "var(--text-secondary)";
  return v >= 0 ? "var(--gain)" : "var(--loss)";
}

export function dirClass(v: number | null | undefined): string {
  if (v == null) return "text-secondary";
  return v >= 0 ? "text-gain" : "text-loss";
}
