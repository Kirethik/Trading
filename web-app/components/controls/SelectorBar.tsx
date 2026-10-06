"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useBacktestStore } from "@/store/backtest";

const TIMEFRAMES = [
  { value: "1Day", label: "Daily" },
  { value: "4Hour", label: "4-Hour" },
];

function Select({ label, value, onChange, children }: {
  label: string; value: string;
  onChange: (v: string) => void; children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
        {label}
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{
          background: "var(--bg-elevated)", border: "1px solid var(--border)",
          color: "var(--text-primary)", borderRadius: 6, padding: "6px 10px",
          fontSize: 13, cursor: "pointer", outline: "none", minWidth: 120,
        }}
      >
        {children}
      </select>
    </div>
  );
}

function NumberInput({ label, value, onChange, prefix }: {
  label: string; value: number; onChange: (v: number) => void; prefix?: string;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
        {label}
      </span>
      <div className="flex items-center" style={{
        background: "var(--bg-elevated)", border: "1px solid var(--border)",
        borderRadius: 6, overflow: "hidden",
      }}>
        {prefix && (
          <span style={{ padding: "6px 8px 6px 10px", color: "var(--text-muted)", fontSize: 13 }}>{prefix}</span>
        )}
        <input
          type="number"
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          style={{
            background: "transparent", border: "none", color: "var(--text-primary)",
            padding: prefix ? "6px 10px 6px 0" : "6px 10px",
            fontSize: 13, outline: "none", width: 90,
          }}
        />
      </div>
    </div>
  );
}

function DateInput({ label, value, onChange }: {
  label: string; value: string; onChange: (v: string) => void;
}) {
  return (
    <div className="flex flex-col gap-1">
      <span style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
        {label}
      </span>
      <input
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        style={{
          background: "var(--bg-elevated)", border: "1px solid var(--border)",
          color: "var(--text-primary)", borderRadius: 6, padding: "6px 10px",
          fontSize: 13, outline: "none", colorScheme: "dark",
        }}
      />
    </div>
  );
}

export function SelectorBar({ onRefresh, isFetching }: { onRefresh: () => void; isFetching: boolean }) {
  const { params, setParams } = useBacktestStore();

  const { data: assetsData } = useQuery({
    queryKey: ["assets"],
    queryFn: api.assets,
  });

  const { data: strategiesData } = useQuery({
    queryKey: ["strategies"],
    queryFn: api.strategies,
  });

  const assets = assetsData?.assets ?? [];
  const strategies = strategiesData?.strategies ?? [];
  const strategy = strategies.find((s) => s.key === params.strategy);

  // group assets
  const cryptos = assets.filter((a) => a.class === "crypto");
  const stocks  = assets.filter((a) => a.class === "stock");
  const etfs    = assets.filter((a) => a.class === "etf");

  return (
    <div
      className="flex flex-wrap items-end gap-3 p-4"
      style={{ background: "var(--bg-surface)", borderBottom: "1px solid var(--border)" }}
    >
      {/* Asset */}
      <Select label="Asset" value={params.symbol} onChange={(v) => setParams({ symbol: v })}>
        {cryptos.length > 0 && <optgroup label="Crypto">{cryptos.map((a) => <option key={a.symbol} value={a.symbol}>{a.label} ({a.symbol})</option>)}</optgroup>}
        {stocks.length > 0  && <optgroup label="Stocks">{stocks.map((a) => <option key={a.symbol} value={a.symbol}>{a.label}</option>)}</optgroup>}
        {etfs.length > 0    && <optgroup label="ETFs">{etfs.map((a) => <option key={a.symbol} value={a.symbol}>{a.label}</option>)}</optgroup>}
      </Select>

      {/* Strategy */}
      <Select label="Strategy" value={params.strategy} onChange={(v) => setParams({ strategy: v, params: {} })}>
        {strategies.map((s) => (
          <option key={s.key} value={s.key}>{s.label}{s.needs_regime ? " ✦" : ""}</option>
        ))}
      </Select>

      {/* Timeframe */}
      <Select label="Timeframe" value={params.timeframe} onChange={(v) => setParams({ timeframe: v })}>
        {TIMEFRAMES.map((t) => <option key={t.value} value={t.value}>{t.label}</option>)}
      </Select>

      {/* Amount */}
      <NumberInput label="Amount" value={params.amount} onChange={(v) => setParams({ amount: v })} prefix="$" />

      {/* Dates */}
      <DateInput label="From" value={params.start_date} onChange={(v) => setParams({ start_date: v })} />
      <DateInput label="To"   value={params.end_date}   onChange={(v) => setParams({ end_date: v })} />

      {/* Strategy params (compact sliders) — changes auto-run the backtest */}
      {strategy?.params?.map((p) => (
        <div key={p.name} className="flex flex-col gap-1">
          <span style={{ fontSize: 10, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>
            {p.label} <span style={{ color: "var(--text-secondary)" }}>({params.params[p.name] ?? p.default})</span>
          </span>
          <input
            type="range"
            min={p.min} max={p.max} step={p.step}
            value={params.params[p.name] ?? p.default}
            onChange={(e) => setParams({ params: { ...params.params, [p.name]: Number(e.target.value) } })}
            style={{ width: 100, accentColor: "var(--accent)" }}
          />
        </div>
      ))}

      {/* Refresh button — results update automatically as you change inputs;
          this forces a fresh recompute with the latest data (bypasses caches) */}
      <button
        onClick={onRefresh}
        disabled={isFetching}
        title="Results update automatically. Click to force a fresh recompute with the latest data."
        style={{
          background: "transparent",
          color: isFetching ? "var(--text-muted)" : "var(--accent)",
          border: "1px solid var(--border)", borderRadius: 6, padding: "7px 16px",
          fontSize: 13, fontWeight: 600, cursor: isFetching ? "not-allowed" : "pointer",
          transition: "all 0.15s", alignSelf: "flex-end",
          opacity: isFetching ? 0.7 : 1,
        }}
      >
        {isFetching ? "Recalculating…" : "↻ Refresh"}
      </button>
    </div>
  );
}
