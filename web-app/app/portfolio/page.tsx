"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { fmt, dirClass } from "@/lib/format";
import { AuthGate } from "@/components/ui/AuthGate";
import { SectionTitle, Muted, StatCard, Panel, Table, Skeleton } from "@/components/ui/data";

function PortfolioInner() {
  const portfolio = useQuery({ queryKey: ["portfolio"], queryFn: api.portfolio, refetchInterval: 10000 });
  const trades = useQuery({ queryKey: ["trades", 300], queryFn: () => api.trades(300) });

  const p = portfolio.data;
  const t = p?.totals;

  return (
    <main style={{ flex: 1, padding: 16, display: "flex", flexDirection: "column", gap: 16, maxWidth: 1600, margin: "0 auto", width: "100%" }}>
      <SectionTitle>
        Capital &amp; performance{" "}
        {p ? <Muted>· updated {new Date(p.as_of).toLocaleTimeString()}{p.live ? "" : " · prices stale"}</Muted> : null}
      </SectionTitle>

      {!t ? (
        <Skeleton h={92} />
      ) : (() => {
        const net_pnl = t.total_realized_pnl + t.total_unrealized_pnl;
        const net_pnl_pct = t.total_invested ? net_pnl / t.total_invested : 0;
        return (
          <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
            <StatCard label="Capital allocated" value={fmt.usd2(t.total_invested)} />
            <StatCard label="Cash (available)" value={fmt.usd2(t.total_cash)} />
            <StatCard label="In positions" value={fmt.usd2(t.total_market_value)} />
            <StatCard label="Total equity" value={fmt.usd2(t.total_equity)} />
            <StatCard label="Net P&L (realized + open)" value={`${fmt.usd2(net_pnl)} (${fmt.pct2(net_pnl_pct)})`} cls={dirClass(net_pnl)} />
            <StatCard label="Realized P&L" value={fmt.usd2(t.total_realized_pnl)} cls={dirClass(t.total_realized_pnl)} />
            <StatCard label="Unrealized P&L" value={fmt.usd2(t.total_unrealized_pnl)} cls={dirClass(t.total_unrealized_pnl)} />
            <StatCard label="Open positions" value={String(t.open_positions)} />
          </div>
        );
      })()}

      <Panel title="Open positions" note="live prices, marked to market">
        <Table
          cols={["strategy", "symbol", "qty", "entry", "price", "value", "unrealized_pnl", "unrealized_pnl_pct"]}
          rows={p?.positions ?? []}
          fmt={(row, c) => {
            if (c === "entry" || c === "price" || c === "value" || c === "unrealized_pnl") return fmt.usd2(row[c]);
            if (c === "unrealized_pnl_pct") return fmt.pct2(row[c]);
            if (c === "qty") return fmt.ratio(row[c], 4);
            return String(row[c] ?? "");
          }}
          cls={(row, c) => (c === "unrealized_pnl" || c === "unrealized_pnl_pct") ? dirClass(row.unrealized_pnl) : ""}
          empty="No open positions"
        />
      </Panel>

      <Panel
        title="Executed trades"
        note={trades.data
          ? `${trades.data.summary.fills} fills · ${trades.data.summary.approved} approved · ${trades.data.summary.blocked} blocked (recent window)`
          : "order pipeline from the trades log"}
      >
        <Table
          cols={["ts", "strategy", "symbol", "side", "requested", "final", "approved", "reason"]}
          rows={trades.data?.rows ?? []}
          fmt={(row, c) => {
            if (c === "ts") return String(row.ts ?? "").replace("T", " ").slice(0, 19);
            if (c === "requested" || c === "final") return row[c] == null ? "—" : fmt.pct2(row[c]);
            if (c === "approved") return row.approved ? "✓ approved" : "✗ blocked";
            if (c === "side") return String(row.side ?? "").toUpperCase();
            return String(row[c] ?? "");
          }}
          cls={(row, c) => (c === "approved" ? (row.approved ? "text-gain" : "text-loss") : "")}
          empty="No order activity recorded yet"
        />
      </Panel>
    </main>
  );
}

export default function PortfolioPage() {
  return <AuthGate><PortfolioInner /></AuthGate>;
}
