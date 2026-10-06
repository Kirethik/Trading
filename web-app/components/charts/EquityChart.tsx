"use client";
import { useEffect, useRef, useState, useCallback, useMemo } from "react";
import type { BacktestPayload, TradeRegion } from "@/lib/api";

interface Props {
  data: BacktestPayload | null;
  height?: number;
}

type RangePeriod = "1M" | "3M" | "6M" | "1Y" | "ALL";

// ── tiny local helpers ──────────────────────────────────────────────────────
function fmtDate(tsMs: number): string {
  return new Date(tsMs).toISOString().slice(0, 10);
}
function fmtPrice(p: number): string {
  return p >= 1000
    ? p.toLocaleString("en-US", { maximumFractionDigits: 2 })
    : p < 1
    ? p.toPrecision(4)
    : p.toFixed(2);
}

// ── trade summary subcomponents ─────────────────────────────────────────────
function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ padding: "2px 14px", textAlign: "center" }}>
      <div style={{ fontSize: 10, color: "var(--text-muted)", marginBottom: 2, whiteSpace: "nowrap" }}>{label}</div>
      <div style={{ fontSize: 13, fontWeight: 600, color: color ?? "var(--text-secondary)", fontVariantNumeric: "tabular-nums" }}>{value}</div>
    </div>
  );
}
function VRule() {
  return <div style={{ width: 1, background: "var(--border-soft)", alignSelf: "stretch", margin: "4px 0" }} />;
}

// ── main component ──────────────────────────────────────────────────────────
export function EquityChart({ data, height = 340 }: Props) {
  // chart refs
  const wrapperRef  = useRef<HTMLDivElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef   = useRef<HTMLCanvasElement>(null);
  const chartRef    = useRef<any>(null);
  const stratRef    = useRef<any>(null);
  const benchRef    = useRef<any>(null);
  const ddRef       = useRef<any>(null);
  const markersRef  = useRef<any>(null);

  // morph animation
  const prevStratRef = useRef<{ time: number; value: number }[] | null>(null);
  const prevDDRef    = useRef<{ time: number; value: number }[] | null>(null);
  const rafRef       = useRef<number>(0);

  // live trades list read by drawOverlay without re-rendering
  const tradesRef    = useRef<TradeRegion[]>([]);

  // toolbar toggles
  const [showBench,   setShowBench]   = useState(true);
  const [showDD,      setShowDD]      = useState(true);
  const [showRegions, setShowRegions] = useState(true);
  const [range,       setRange]       = useState<RangePeriod>("ALL");

  // hover tooltip
  const [hoveredTrade, setHoveredTrade] = useState<TradeRegion | null>(null);
  const [tooltipPos,   setTooltipPos]   = useState({ x: 0, y: 0 });

  // ── canvas overlay drawing ─────────────────────────────────────────────────
  // Closed over showRegions; stored in a ref so the chart's range-change
  // subscription always invokes the latest version without re-subscribing.
  const drawOverlay = useCallback(() => {
    const canvas   = canvasRef.current;
    const chartObj = chartRef.current;
    if (!canvas || !chartObj) return;
    const { chart } = chartObj;

    const dpr = window.devicePixelRatio || 1;
    const w   = canvas.clientWidth;
    const h   = canvas.clientHeight;
    if (!w || !h) return;

    canvas.width  = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d")!;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, w, h);

    if (!showRegions || !tradesRef.current.length) return;

    const timeScale = chart.timeScale();
    // Leave ~28 px at the bottom for the time axis; don't shade into it.
    const paneH = Math.max(h - 28, 0);

    for (const trade of tradesRef.current) {
      const t1 = Math.floor(trade.entry_ts / 1000);
      const t2 = Math.floor(trade.exit_ts  / 1000);
      const x1 = timeScale.timeToCoordinate(t1);
      const x2 = timeScale.timeToCoordinate(t2);

      // If BOTH timestamps are off-screen, skip.
      if (x1 === null && x2 === null) continue;
      const left  = x1 !== null ? x1 : 0;
      const right = x2 !== null ? x2 : w;
      if (right < 0 || left > w) continue;

      const cL   = Math.max(0, Math.min(left, right));
      const cR   = Math.min(w, Math.max(left, right));
      const regW = cR - cL;
      if (regW < 1) continue;

      const [fillRgba, border] = trade.is_open
        ? ["rgba(234,179,8,0.09)",    "#EAB308"]
        : trade.profitable
        ? ["rgba(16,185,129,0.11)",  "#10B981"]
        : ["rgba(244,63,94,0.11)",   "#F43F5E"];

      // fill
      ctx.fillStyle = fillRgba;
      ctx.fillRect(cL, 0, regW, paneH);

      // entry border
      if (x1 !== null && x1 >= 0 && x1 <= w) {
        ctx.save();
        ctx.strokeStyle  = border;
        ctx.lineWidth    = 1.5;
        ctx.globalAlpha  = 0.55;
        ctx.setLineDash([]);
        ctx.beginPath();
        ctx.moveTo(x1, 0);
        ctx.lineTo(x1, paneH);
        ctx.stroke();
        ctx.restore();
      }

      // exit border (dashed for open positions)
      if (x2 !== null && x2 >= 0 && x2 <= w) {
        ctx.save();
        ctx.strokeStyle  = border;
        ctx.lineWidth    = trade.is_open ? 1 : 1.5;
        ctx.globalAlpha  = trade.is_open ? 0.35 : 0.55;
        ctx.setLineDash(trade.is_open ? [4, 3] : []);
        ctx.beginPath();
        ctx.moveTo(x2, 0);
        ctx.lineTo(x2, paneH);
        ctx.stroke();
        ctx.restore();
      }

      // label (return % or "Open") centered in the region
      if (regW > 44 && paneH > 18) {
        const label = trade.is_open
          ? "Open"
          : trade.return_pct != null
          ? `${trade.return_pct >= 0 ? "+" : ""}${trade.return_pct.toFixed(1)}%`
          : "";
        if (label) {
          ctx.save();
          ctx.font        = `500 10px -apple-system,system-ui,sans-serif`;
          ctx.fillStyle   = border;
          ctx.globalAlpha = 0.70;
          ctx.textAlign   = "center";
          ctx.textBaseline = "top";
          ctx.fillText(label, cL + regW / 2, 5);
          ctx.restore();
        }
      }
    }
  }, [showRegions]);

  // Keep a stable ref so range-change subscription never goes stale.
  const drawOverlayRef = useRef(drawOverlay);
  useEffect(() => { drawOverlayRef.current = drawOverlay; }, [drawOverlay]);

  // ── chart initialisation (once) ───────────────────────────────────────────
  useEffect(() => {
    if (!containerRef.current) return;
    let chart: any;

    import("lightweight-charts").then((lc) => {
      const { createChart, ColorType, CrosshairMode, LineSeries, AreaSeries, createSeriesMarkers } = lc;

      chart = createChart(containerRef.current!, {
        width: containerRef.current!.clientWidth,
        height,
        layout: {
          background: { type: ColorType.Solid, color: "transparent" },
          textColor: "#8891A0",
        },
        grid: {
          vertLines: { color: "#1C2028" },
          horzLines: { color: "#1C2028" },
        },
        crosshair:      { mode: CrosshairMode.Normal },
        rightPriceScale: { borderColor: "#232830" },
        timeScale:       { borderColor: "#232830", timeVisible: false },
        handleScroll: true,
        handleScale:  true,
      });

      const strat = chart.addSeries(LineSeries, {
        color: "#6366F1", lineWidth: 2,
        priceLineVisible: false, lastValueVisible: false, title: "Strategy",
      });
      stratRef.current  = strat;
      markersRef.current = createSeriesMarkers(strat, []);

      benchRef.current = chart.addSeries(LineSeries, {
        color: "#8891A0", lineWidth: 1.5, lineStyle: 2,
        priceLineVisible: false, lastValueVisible: false, title: "Buy & Hold",
      });

      ddRef.current = chart.addSeries(AreaSeries, {
        lineColor: "#F43F5E",
        topColor: "rgba(244,63,94,0.18)",
        bottomColor: "rgba(244,63,94,0.02)",
        lineWidth: 1, priceScaleId: "dd",
        priceLineVisible: false, lastValueVisible: false, title: "Drawdown",
      });
      chart.priceScale("dd").applyOptions({ position: "none" });

      // Redraw overlay on zoom/pan.
      chart.timeScale().subscribeVisibleTimeRangeChange(() => drawOverlayRef.current());

      const ro = new ResizeObserver(() => {
        if (containerRef.current) {
          chart.applyOptions({ width: containerRef.current.clientWidth });
          requestAnimationFrame(() => drawOverlayRef.current());
        }
      });
      ro.observe(containerRef.current!);
      chartRef.current = { chart, ro };
    });

    return () => {
      cancelAnimationFrame(rafRef.current);
      chartRef.current?.ro.disconnect();
      chart?.remove();
      chartRef.current  = null;
      markersRef.current = null;
    };
  }, [height]);

  // ── data update ───────────────────────────────────────────────────────────
  useEffect(() => {
    if (!data || !stratRef.current) return;

    const toSeries = (pts: [number, number][]) =>
      pts.map(([ts, v]) => ({ time: Math.floor(ts / 1000), value: v }));

    const target   = toSeries(data.equity);
    const ddTarget = toSeries(showDD ? data.drawdown : []);
    benchRef.current.setData((showBench ? toSeries(data.benchmark.equity) : []) as any);

    // Buy/sell arrow markers on the strategy series.
    if (markersRef.current) {
      const markers = (data.signals ?? []).map((sig) => ({
        time:     Math.floor(sig.ts / 1000),
        position: sig.side === "buy" ? "belowBar" : "aboveBar",
        color:    sig.side === "buy" ? "#10B981" : "#F43F5E",
        shape:    sig.side === "buy" ? "arrowUp"  : "arrowDown",
        size:     1.2,
      }));
      markersRef.current.setMarkers(markers as any);
    }

    // Trade regions for canvas overlay.
    tradesRef.current = data.trades ?? [];

    // Morph or hard-swap equity / drawdown curves.
    const prev     = prevStratRef.current;
    const sameAxis = !!prev && prev.length === target.length && prev.length > 1 &&
      prev[0].time === target[0].time &&
      prev[prev.length - 1].time === target[target.length - 1].time;

    cancelAnimationFrame(rafRef.current);

    if (!sameAxis) {
      stratRef.current.setData(target as any);
      ddRef.current.setData(ddTarget as any);
    } else {
      const prevDD  = prevDDRef.current ?? [];
      const ddMorph = prevDD.length === ddTarget.length && ddTarget.length > 1;
      if (!ddMorph) ddRef.current.setData(ddTarget as any);

      const start = Date.now();
      const dur   = 420;
      const tick  = () => {
        const t = Math.min((Date.now() - start) / dur, 1);
        const e = 1 - Math.pow(1 - t, 3); // ease-out cubic
        stratRef.current.setData(
          target.map((p, i) => ({ time: p.time, value: prev![i].value + (p.value - prev![i].value) * e })) as any
        );
        if (ddMorph) {
          ddRef.current.setData(
            ddTarget.map((p, i) => ({ time: p.time, value: prevDD[i].value + (p.value - prevDD[i].value) * e })) as any
          );
        }
        if (t < 1) {
          rafRef.current = requestAnimationFrame(tick);
        } else {
          stratRef.current.setData(target as any);
          ddRef.current.setData(ddTarget as any);
        }
      };
      rafRef.current = requestAnimationFrame(tick);
    }

    prevStratRef.current = target;
    prevDDRef.current    = ddTarget;
    applyRange(range, data.equity);
    requestAnimationFrame(() => drawOverlayRef.current());
  }, [data, showBench, showDD]); // eslint-disable-line react-hooks/exhaustive-deps

  // Redraw overlay when the "Trades" toggle flips.
  useEffect(() => {
    requestAnimationFrame(() => drawOverlayRef.current());
  }, [showRegions]);

  // ── range helpers ─────────────────────────────────────────────────────────
  function applyRange(r: RangePeriod, equity: [number, number][]) {
    if (!chartRef.current || !equity.length) return;
    const { chart } = chartRef.current;
    if (r === "ALL") { chart.timeScale().fitContent(); return; }
    const now = equity[equity.length - 1][0];
    const ms  = { "1M": 30, "3M": 90, "6M": 180, "1Y": 365 }[r] * 86_400_000;
    chart.timeScale().setVisibleRange({
      from: Math.floor((now - ms) / 1000),
      to:   Math.floor(now / 1000),
    } as any);
  }

  function handleRange(r: RangePeriod) {
    setRange(r);
    applyRange(r, data?.equity ?? []);
  }

  // ── hover tooltip ─────────────────────────────────────────────────────────
  function handleMouseMove(e: React.MouseEvent<HTMLDivElement>) {
    const chart = chartRef.current?.chart;
    if (!chart || !showRegions || !tradesRef.current.length) {
      if (hoveredTrade) setHoveredTrade(null);
      return;
    }
    const rect      = wrapperRef.current!.getBoundingClientRect();
    const x         = e.clientX - rect.left;
    const timeScale = chart.timeScale();

    for (const trade of tradesRef.current) {
      const x1 = timeScale.timeToCoordinate(Math.floor(trade.entry_ts / 1000));
      const x2 = timeScale.timeToCoordinate(Math.floor(trade.exit_ts  / 1000));
      if (x1 === null || x2 === null) continue;
      if (x >= Math.min(x1, x2) && x <= Math.max(x1, x2)) {
        setHoveredTrade(trade);
        setTooltipPos({ x: e.clientX - rect.left, y: e.clientY - rect.top });
        return;
      }
    }
    if (hoveredTrade) setHoveredTrade(null);
  }

  // ── trade summary stats ───────────────────────────────────────────────────
  const stats = useMemo(() => {
    const all     = data?.trades ?? [];
    const closed  = all.filter((t) => !t.is_open);
    const winners = closed.filter((t) => t.profitable);
    const open    = all.filter((t) => t.is_open);
    const avgHold = closed.length
      ? closed.reduce((s, t) => s + t.holding_days, 0) / closed.length
      : 0;
    return { total: all.length, closed: closed.length, winners: winners.length,
             losers: closed.length - winners.length,
             winRate: closed.length ? winners.length / closed.length : 0,
             avgHold, open: open.length };
  }, [data?.trades]);

  // ── tooltip positioning ───────────────────────────────────────────────────
  const wrapW   = wrapperRef.current?.clientWidth ?? 600;
  const tipLeft = tooltipPos.x > wrapW / 2
    ? Math.max(8, tooltipPos.x - 216)
    : tooltipPos.x + 12;
  const tipTop  = Math.max(8, Math.min(tooltipPos.y - 90, height - 200));

  const RANGES: RangePeriod[] = ["1M", "3M", "6M", "1Y", "ALL"];

  return (
    <div className="rounded-lg overflow-hidden"
         style={{ background: "var(--bg-surface)", border: "1px solid var(--border)" }}>

      {/* ── toolbar ── */}
      <div className="flex items-center justify-between px-4 py-3"
           style={{ borderBottom: "1px solid var(--border-soft)" }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-secondary)" }}>
          Equity Curve
        </span>
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-3" style={{ fontSize: 12 }}>

            <button onClick={() => setShowBench(!showBench)}
                    className="flex items-center gap-1.5 transition-opacity"
                    style={{ opacity: showBench ? 1 : 0.4, color: "#8891A0" }}>
              <span style={{ width: 16, height: 2, background: "#8891A0",
                             display: "inline-block", borderRadius: 1 }} />
              Buy &amp; Hold
            </button>

            <button onClick={() => setShowDD(!showDD)}
                    className="flex items-center gap-1.5 transition-opacity"
                    style={{ opacity: showDD ? 1 : 0.4, color: "#F43F5E" }}>
              <span style={{ width: 16, height: 2, background: "#F43F5E",
                             display: "inline-block", borderRadius: 1 }} />
              Drawdown
            </button>

            <button onClick={() => setShowRegions(!showRegions)}
                    className="flex items-center gap-1.5 transition-opacity"
                    style={{ opacity: showRegions ? 1 : 0.4, color: "#10B981" }}>
              <span style={{
                width: 12, height: 12,
                background: "rgba(16,185,129,0.22)",
                border: "1.5px solid #10B981",
                display: "inline-block", borderRadius: 2,
              }} />
              Trades
            </button>
          </div>

          {/* range buttons */}
          <div className="flex gap-1">
            {RANGES.map((r) => (
              <button key={r} onClick={() => handleRange(r)} style={{
                fontSize: 11, padding: "2px 7px", borderRadius: 4,
                background: range === r ? "var(--accent-bg)" : "transparent",
                color:      range === r ? "var(--accent)"    : "var(--text-muted)",
                border:     range === r ? "1px solid rgba(99,102,241,0.3)" : "1px solid transparent",
                cursor: "pointer",
              }}>{r}</button>
            ))}
          </div>
        </div>
      </div>

      {/* ── chart + canvas overlay ── */}
      <div ref={wrapperRef}
           style={{ position: "relative", width: "100%", height }}
           onMouseMove={handleMouseMove}
           onMouseLeave={() => setHoveredTrade(null)}>

        {/* Lightweight Charts renders its canvas inside this div */}
        <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />

        {/* Canvas overlay — sibling after chart div → higher paint order */}
        <canvas ref={canvasRef}
                style={{ position: "absolute", inset: 0, pointerEvents: "none",
                         width: "100%", height: "100%" }} />

        {/* Hover tooltip */}
        {hoveredTrade && (
          <div style={{
            position: "absolute",
            left: tipLeft,
            top:  tipTop,
            zIndex: 20,
            background: "var(--bg-elevated, #161b22)",
            border: `1px solid ${
              hoveredTrade.is_open ? "#EAB308"
                : hoveredTrade.profitable ? "#10B981"
                : "#F43F5E"
            }`,
            borderRadius: 8,
            padding: "10px 14px",
            fontSize: 12,
            lineHeight: 1.8,
            minWidth: 204,
            pointerEvents: "none",
            boxShadow: "0 8px 32px rgba(0,0,0,0.55)",
          }}>
            {/* header */}
            <div style={{ display: "flex", justifyContent: "space-between",
                          alignItems: "center", marginBottom: 6 }}>
              <span style={{ fontWeight: 700, fontSize: 13,
                             color: "var(--text-primary, #E2E8F0)" }}>
                {hoveredTrade.symbol}
              </span>
              <span style={{
                fontSize: 10, padding: "2px 7px", borderRadius: 4, fontWeight: 600,
                background: hoveredTrade.is_open
                  ? "rgba(234,179,8,0.15)"
                  : hoveredTrade.profitable ? "rgba(16,185,129,0.15)" : "rgba(244,63,94,0.15)",
                color: hoveredTrade.is_open ? "#EAB308"
                  : hoveredTrade.profitable ? "#10B981" : "#F43F5E",
              }}>
                {hoveredTrade.is_open ? "Open Position" : hoveredTrade.profitable ? "Win" : "Loss"}
              </span>
            </div>
            {/* rows */}
            {([ ["Entry",   `${fmtDate(hoveredTrade.entry_ts)} · $${fmtPrice(hoveredTrade.entry_price)}`],
                ...(hoveredTrade.is_open ? [] : [[
                  "Exit",
                  `${fmtDate(hoveredTrade.exit_ts)} · $${fmtPrice(hoveredTrade.exit_price!)}`,
                ]]),
                ["Hold",    `${hoveredTrade.holding_days.toFixed(0)} days`],
              ] as [string, string][]).map(([label, val]) => (
              <div key={label} style={{ display: "flex", justifyContent: "space-between", gap: 12 }}>
                <span style={{ color: "var(--text-muted, #636B7C)" }}>{label}</span>
                <span style={{ color: "var(--text-primary, #E2E8F0)",
                               fontVariantNumeric: "tabular-nums" }}>{val}</span>
              </div>
            ))}
            {hoveredTrade.return_pct != null && (
              <div style={{ display: "flex", justifyContent: "space-between", gap: 12,
                            borderTop: "1px solid var(--border-soft)", marginTop: 6, paddingTop: 6 }}>
                <span style={{ color: "var(--text-muted, #636B7C)" }}>Return</span>
                <span style={{
                  fontWeight: 700, fontSize: 13, fontVariantNumeric: "tabular-nums",
                  color: hoveredTrade.profitable ? "#10B981" : "#F43F5E",
                }}>
                  {hoveredTrade.return_pct >= 0 ? "+" : ""}
                  {hoveredTrade.return_pct.toFixed(2)}%
                </span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* ── trade summary strip ── */}
      {!!data?.trades?.length && (
        <div style={{ borderTop: "1px solid var(--border-soft)",
                      padding: "8px 8px", overflowX: "auto" }}>
          <div style={{ display: "flex", alignItems: "center", minWidth: "max-content" }}>
            <Stat label="Total Trades"  value={`${stats.total}`} />
            <VRule />
            <Stat label="Winners" value={`${stats.winners}`} color="#10B981" />
            <VRule />
            <Stat label="Losers"  value={`${stats.losers}`}  color="#F43F5E" />
            <VRule />
            <Stat label="Win Rate"
                  value={stats.closed ? `${(stats.winRate * 100).toFixed(1)}%` : "—"}
                  color={stats.winRate >= 0.5 ? "#10B981" : stats.winRate > 0 ? "#F43F5E" : undefined} />
            <VRule />
            <Stat label="Avg Hold"
                  value={stats.closed ? `${stats.avgHold.toFixed(0)}d` : "—"} />
            {stats.open > 0 && (
              <>
                <VRule />
                <Stat label="Still Open" value={`${stats.open}`} color="#EAB308" />
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
