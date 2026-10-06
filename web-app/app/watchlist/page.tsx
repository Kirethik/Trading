"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { fmt } from "@/lib/format";
import { AuthGate } from "@/components/ui/AuthGate";
import { SectionTitle, Muted, StatCard, Panel, Table, Empty } from "@/components/ui/data";

function WatchlistInner() {
  const watch = useQuery({ queryKey: ["watchlist"], queryFn: api.watchlist, refetchInterval: 15000 });
  const learn = useQuery({ queryKey: ["learning"], queryFn: api.learning, refetchInterval: 30000 });

  const w = watch.data;
  const l = learn.data;

  const regime = l?.regime ?? null;
  const bicEntries = regime ? Object.entries(regime.bic) : [];
  const bicVals = bicEntries.map(([, v]) => v);
  const minBic = bicVals.length ? Math.min(...bicVals) : 0;
  const maxBic = bicVals.length ? Math.max(...bicVals) : 1;
  const maxReason = l?.risk_reasons.length ? Math.max(...l.risk_reasons.map((r) => r.count)) : 1;

  return (
    <main style={{ flex: 1, padding: 16, display: "flex", flexDirection: "column", gap: 16, maxWidth: 1600, margin: "0 auto", width: "100%" }}>
      <SectionTitle>
        Stocks in consideration{" "}
        {w ? <Muted>· {w.n_symbols} symbols · {w.n_held} currently held</Muted> : null}
      </SectionTitle>

      <Panel title="Universe" note="watched symbols · ● = currently held">
        <Table
          cols={["symbol", "label", "cls", "held", "qty", "held_by"]}
          rows={w?.symbols ?? []}
          fmt={(row, c) => {
            if (c === "held") return row.held ? "● held" : "watching";
            if (c === "qty") return row.qty ? fmt.ratio(row.qty, 4) : "";
            return String(row[c] ?? "");
          }}
          cls={(row, c) => (c === "held" && row.held ? "text-gain" : "")}
          empty="No universe configured"
        />
      </Panel>

      <Panel title="Strategies under consideration" note="governance registry">
        <Table
          cols={["key", "status", "market", "source", "allocation", "last_validated"]}
          rows={w?.strategies ?? []}
          fmt={(row, c) => {
            if (c === "allocation") return fmt.usd(row.allocation);
            return String(row[c] ?? "");
          }}
          empty="No strategies in the registry yet"
        />
      </Panel>

      <SectionTitle>
        What&apos;s being learnt{" "}
        {l ? <Muted>· updated {new Date(l.as_of).toLocaleTimeString()}</Muted> : null}
      </SectionTitle>

      <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
        <StatCard label="Regime model" value={regime ? `${regime.states} states` : "—"} />
        <StatCard label="Last model fit" value={regime?.ts ? regime.ts.slice(0, 10) : "—"} />
        <StatCard label="Risk lessons tracked" value={String(l?.risk_reasons.length ?? 0)} />
        <StatCard label="Strategies with notes" value={String(l?.strategy_insights.length ?? 0)} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <Panel title="HMM model selection" note="BIC by state count — lower is better, ● chosen">
          {!bicEntries.length ? (
            <Empty>no regime fits logged yet</Empty>
          ) : (
            <div className="flex flex-col gap-2" style={{ marginTop: 4 }}>
              {bicEntries.map(([k, v]) => {
                const chosen = regime!.states === Number(k);
                const width = maxBic === minBic ? 100 : 8 + (1 - (v - minBic) / (maxBic - minBic)) * 92;
                return (
                  <div key={k} className="flex items-center gap-3" style={{ fontSize: 12 }}>
                    <span style={{ width: 70, color: chosen ? "var(--gain)" : "var(--text-secondary)" }}>
                      {chosen ? "● " : ""}{k} states
                    </span>
                    <div style={{ flex: 1, background: "var(--bg-elevated)", borderRadius: 4, height: 14 }}>
                      <div style={{ width: `${width}%`, height: "100%", borderRadius: 4, background: chosen ? "var(--gain)" : "var(--accent)" }} />
                    </div>
                    <span className="tabnum" style={{ width: 56, textAlign: "right", color: "var(--text-muted)" }}>{fmt.ratio(v, 0)}</span>
                  </div>
                );
              })}
            </div>
          )}
        </Panel>

        <Panel title="What the risk layer is enforcing" note="reason frequency">
          {!l?.risk_reasons.length ? (
            <Empty>no risk decisions logged yet</Empty>
          ) : (
            <div className="flex flex-col gap-2" style={{ marginTop: 4 }}>
              {l.risk_reasons.map((r) => (
                <div key={r.reason} style={{ fontSize: 12 }}>
                  <div className="flex justify-between" style={{ marginBottom: 2 }}>
                    <span style={{ color: "var(--text-secondary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", maxWidth: "82%" }}>{r.reason}</span>
                    <span className="tabnum" style={{ color: "var(--text-muted)" }}>{r.count}</span>
                  </div>
                  <div style={{ background: "var(--bg-elevated)", borderRadius: 4, height: 8 }}>
                    <div style={{ width: `${(r.count / maxReason) * 100}%`, height: "100%", borderRadius: 4, background: "var(--warn)" }} />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>

      <Panel title="Strategy notes" note="latest governance rationale">
        <Table
          cols={["ts", "key", "decision", "rationale"]}
          rows={l?.strategy_insights ?? []}
          fmt={(row, c) => {
            if (c === "ts") return String(row.ts ?? "").replace("T", " ").slice(0, 19);
            return String(row[c] ?? "");
          }}
          empty="No governance decisions recorded yet"
        />
      </Panel>
    </main>
  );
}

export default function WatchlistPage() {
  return <AuthGate><WatchlistInner /></AuthGate>;
}
