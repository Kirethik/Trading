"use client";
import { useEffect, useRef, useState } from "react";
import type { Metrics } from "@/lib/api";
import { fmt, dirClass } from "@/lib/format";

interface KpiDef {
  label: string;
  value: (m: Metrics, amount: number) => string;
  sub?: (m: Metrics, amount: number) => string;
  colorFn?: (m: Metrics) => string;
}

const KPI_DEFS: KpiDef[] = [
  {
    label: "Portfolio Value",
    value: (m, a) => fmt.usd(a * (1 + m.total_return)),
    sub: (m) => fmt.sign(m.total_return) + " total",
    colorFn: (m) => dirClass(m.total_return),
  },
  {
    label: "Total Return",
    value: (m) => fmt.sign(m.total_return),
    colorFn: (m) => dirClass(m.total_return),
  },
  {
    label: "CAGR",
    value: (m) => fmt.sign(m.cagr),
    colorFn: (m) => dirClass(m.cagr),
  },
  {
    label: "Sharpe Ratio",
    value: (m) => fmt.ratio(m.sharpe),
    colorFn: (m) => m.sharpe >= 1.0 ? "text-gain" : m.sharpe >= 0.5 ? "" : "text-loss",
  },
  {
    label: "Sortino",
    value: (m) => fmt.ratio(m.sortino),
    colorFn: (m) => m.sortino >= 1.0 ? "text-gain" : "",
  },
  {
    label: "Max Drawdown",
    value: (m) => fmt.pct(m.max_drawdown),
    colorFn: (m) => m.max_drawdown < -0.3 ? "text-loss" : m.max_drawdown < -0.15 ? "text-warn" : "text-gain",
  },
  {
    label: "Profit Factor",
    value: (m) => fmt.ratio(m.profit_factor),
    colorFn: (m) => (m.profit_factor ?? 0) >= 1.5 ? "text-gain" : "text-loss",
  },
  {
    label: "Win Rate",
    value: (m) => fmt.pct(m.win_rate),
    colorFn: (m) => (m.win_rate ?? 0) >= 0.5 ? "text-gain" : "text-loss",
  },
  {
    label: "Avg Trade",
    value: (m) => m.avg_trade != null ? fmt.usd2(m.avg_trade) : "—",
    colorFn: (m) => dirClass(m.avg_trade ?? 0),
  },
  {
    label: "Ann. Vol",
    value: (m) => fmt.pct(m.ann_vol),
  },
  {
    label: "Recovery Factor",
    value: (m) => fmt.ratio(m.recovery_factor),
    colorFn: (m) => (m.recovery_factor ?? 0) >= 2 ? "text-gain" : "",
  },
];

function useCountUp(target: string, active: boolean): string {
  const [display, setDisplay] = useState(target);
  const rafRef = useRef<number>(0);

  useEffect(() => {
    if (!active) { setDisplay(target); return; }
    const num = parseFloat(target.replace(/[^0-9.\-]/g, ""));
    if (isNaN(num)) { setDisplay(target); return; }
    const prefix = target.replace(/[\d.\-]/g, "").trim().replace(/\d.*/, "");
    const suffix = target.slice(target.replace(/[^%$]/g, "").length > 0
      ? target.lastIndexOf(target.match(/[%]/)?.toString() ?? "#") : target.length);

    const start = Date.now();
    const dur = 600;
    const from = 0;
    const tick = () => {
      const t = Math.min((Date.now() - start) / dur, 1);
      const ease = 1 - Math.pow(1 - t, 3);
      const cur = from + (num - from) * ease;
      setDisplay(target.replace(/[\d,]+\.?\d*/, cur.toFixed(
        target.includes(".") ? (target.split(".")[1]?.match(/\d+/)?.[0]?.length ?? 0) : 0
      )));
      if (t < 1) rafRef.current = requestAnimationFrame(tick);
      else setDisplay(target);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [target, active]);

  return display;
}

function KpiCard({ def, metrics, amount, animate }: {
  def: KpiDef; metrics: Metrics; amount: number; animate: boolean;
}) {
  const raw = def.value(metrics, amount);
  const displayed = useCountUp(raw, animate);
  const colorCls = def.colorFn?.(metrics) ?? "";
  const sub = def.sub?.(metrics, amount);

  return (
    <div
      className="rounded-lg p-4 flex flex-col gap-1 transition-all duration-200 hover:border-[var(--border)] group"
      style={{
        background: "var(--bg-surface)",
        border: "1px solid var(--border-soft)",
      }}
    >
      <span style={{ color: "var(--text-muted)", fontSize: "11px", letterSpacing: "0.04em", textTransform: "uppercase" }}>
        {def.label}
      </span>
      <span
        className={`tabnum font-semibold text-xl leading-tight ${colorCls}`}
        style={{ fontFamily: "var(--font-geist-mono), monospace" }}
      >
        {displayed}
      </span>
      {sub && (
        <span style={{ color: "var(--text-muted)", fontSize: "12px" }} className="tabnum">
          {sub}
        </span>
      )}
    </div>
  );
}

export function KpiGrid({ metrics, amount }: { metrics: Metrics | null; amount: number }) {
  const [animate, setAnimate] = useState(false);
  useEffect(() => {
    if (metrics) { setAnimate(false); requestAnimationFrame(() => setAnimate(true)); }
  }, [metrics]);

  if (!metrics) {
    return (
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
        {KPI_DEFS.map((d) => (
          <div key={d.label} className="rounded-lg p-4 h-[80px] skeleton" />
        ))}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-6 gap-3">
      {KPI_DEFS.map((d) => (
        <KpiCard key={d.label} def={d} metrics={metrics} amount={amount} animate={animate} />
      ))}
    </div>
  );
}
