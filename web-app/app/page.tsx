"use client";
import { useRef } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useBacktestStore } from "@/store/backtest";
import { useDebounce } from "@/lib/useDebounce";
import { SelectorBar } from "@/components/controls/SelectorBar";
import { KpiGrid } from "@/components/kpi/KpiGrid";
import { EquityChart } from "@/components/charts/EquityChart";
import { TradeExplorer } from "@/components/tables/TradeExplorer";
import { fmt, dirClass } from "@/lib/format";

function MonthlyHeatmap({ monthly }: { monthly: { year: number; month: number; return: number }[] }) {
  if (!monthly.length) return null;
  const MONTHS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
  const years = [...new Set(monthly.map((r) => r.year))].sort();
  const map = new Map(monthly.map((r) => [`${r.year}-${r.month}`, r.return]));
  const max = Math.max(...monthly.map((r) => Math.abs(r.return)));

  return (
    <div className="rounded-lg p-4" style={{ background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-secondary)", display: "block", marginBottom: 12 }}>
        Monthly Returns
      </span>
      <div style={{ overflowX: "auto" }}>
        <table style={{ borderCollapse: "separate", borderSpacing: 2, fontSize: 11 }}>
          <thead>
            <tr>
              <th style={{ color: "var(--text-muted)", textAlign: "left", padding: "0 8px 4px 0", fontWeight: 500 }}>Year</th>
              {MONTHS.map((m) => (
                <th key={m} style={{ color: "var(--text-muted)", padding: "0 2px 4px", fontWeight: 500, textAlign: "center", width: 38 }}>{m}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {years.map((y) => (
              <tr key={y}>
                <td style={{ color: "var(--text-secondary)", padding: "0 8px 2px 0", fontWeight: 600 }}>{y}</td>
                {Array.from({ length: 12 }, (_, i) => {
                  const v = map.get(`${y}-${i + 1}`);
                  const intensity = v != null ? Math.min(Math.abs(v) / max, 1) : 0;
                  const bg = v == null ? "transparent"
                    : v >= 0
                    ? `rgba(16,185,129,${0.15 + intensity * 0.65})`
                    : `rgba(244,63,94,${0.15 + intensity * 0.65})`;
                  return (
                    <td key={i} style={{
                      width: 38, height: 26, background: bg,
                      borderRadius: 3, textAlign: "center",
                      color: v == null ? "var(--text-muted)" : v >= 0 ? "#6EE7B7" : "#FDA4AF",
                      fontVariantNumeric: "tabular-nums",
                    }}>
                      {v != null ? `${(v * 100).toFixed(1)}` : ""}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function RegimeBreakdown({ rows }: { rows: { regime: string; sharpe: number; ann_return: number; bars: number }[] }) {
  if (!rows.length) return null;
  return (
    <div className="rounded-lg p-4" style={{ background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
      <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-secondary)", display: "block", marginBottom: 10 }}>
        Regime Attribution
      </span>
      <div className="flex flex-col gap-2">
        {rows.map((r) => (
          <div key={r.regime} className="flex items-center justify-between" style={{ fontSize: 12 }}>
            <span style={{ color: "var(--text-secondary)", minWidth: 120 }}>{r.regime}</span>
            <span className={`tabnum ${dirClass(r.ann_return)}`}>{fmt.sign(r.ann_return)} ann.</span>
            <span className={`tabnum ${dirClass(r.sharpe)}`}>{fmt.ratio(r.sharpe)} Sharpe</span>
            <span style={{ color: "var(--text-muted)" }}>{r.bars} bars</span>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function DashboardPage() {
  const params = useBacktestStore((s) => s.params);
  const toRequest = useBacktestStore((s) => s.toRequest);

  // canonical request → stable JSON key → debounced so a slider drag fires one
  // backtest when the user pauses (~350ms), not one request per pixel.
  const reqKey = JSON.stringify(toRequest());
  const debouncedKey = useDebounce(reqKey, 350);

  // a one-shot "force fresh compute" flag consumed by the next queryFn run
  const forceRef = useRef(false);

  const query = useQuery({
    queryKey: ["backtest", debouncedKey],
    queryFn: () => {
      const force = forceRef.current;
      forceRef.current = false;
      return api.backtest({ ...JSON.parse(debouncedKey), force_refresh: force });
    },
    // keep the previous result on screen while the next one loads (smooth
    // transition instead of a blank skeleton); cache every param combo so
    // dragging a slider back to a prior value snaps in instantly.
    placeholderData: keepPreviousData,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    retry: 1,
  });

  const result = query.data ?? null;
  const isFetching = query.isFetching;
  const isFirstLoad = query.isLoading; // no data yet (nothing to keep) + fetching
  const error = query.error ? (query.error as Error).message : null;

  // "↻ Refresh" forces a fresh compute (bypasses both the client and server caches)
  function handleRefresh() {
    forceRef.current = true;
    query.refetch();
  }

  return (
    <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
      <SelectorBar onRefresh={handleRefresh} isFetching={isFetching} />

      {/* slim indeterminate progress bar while a recompute is in flight */}
      <div style={{ height: 2, overflow: "hidden" }}>
        {isFetching && <div className="bt-progress" />}
      </div>

      <main style={{ flex: 1, padding: "16px", display: "flex", flexDirection: "column", gap: 16, maxWidth: 1600, margin: "0 auto", width: "100%" }}>
        {/* error with no prior result to fall back on */}
        {error && !result && (
          <div style={{
            background: "var(--loss-bg)", border: "1px solid rgba(244,63,94,0.3)",
            borderRadius: 8, padding: "12px 16px", color: "var(--loss)", fontSize: 13,
          }}>
            {error}
          </div>
        )}

        {/* first-load skeleton — only before we have any result to keep */}
        {isFirstLoad && (
          <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
              {Array.from({ length: 11 }).map((_, i) => (
                <div key={i} style={{ height: 80, borderRadius: 8 }} className="skeleton" />
              ))}
            </div>
            <div style={{ height: 340, borderRadius: 8 }} className="skeleton" />
            <div style={{ height: 120, borderRadius: 8 }} className="skeleton" />
          </div>
        )}

        {/* results — stay mounted across re-fetches (placeholderData) and dim
            while a new run is in flight, so the change is visible as a transition */}
        {result && (
          <div style={{
            display: "flex", flexDirection: "column", gap: 16,
            opacity: isFetching ? 0.5 : 1,
            transition: "opacity 0.25s ease",
            pointerEvents: isFetching ? "none" : "auto",
          }}>
            <div style={{ display: "flex", justifyContent: "flex-end", alignItems: "center", minHeight: 14 }}>
              {isFetching ? (
                <span style={{ fontSize: 11, color: "var(--accent)" }}>Recalculating…</span>
              ) : result.from_cache ? (
                <span style={{ fontSize: 11, color: "var(--text-muted)" }}>⚡ Served from cache</span>
              ) : null}
            </div>

            <KpiGrid metrics={result.metrics} amount={result.amount ?? params.amount} />

            <EquityChart data={result} height={340} />

            <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
              <MonthlyHeatmap monthly={result.monthly} />
              <RegimeBreakdown rows={result.per_regime as any} />
            </div>

            <TradeExplorer data={result} />
          </div>
        )}

        {/* refresh failed but we still show the prior result above */}
        {error && result && (
          <div style={{ fontSize: 12, color: "var(--loss)", textAlign: "center" }}>
            Couldn’t refresh: {error}
          </div>
        )}
      </main>
    </div>
  );
}
