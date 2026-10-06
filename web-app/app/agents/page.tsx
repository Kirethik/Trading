"use client";
import { useQuery } from "@tanstack/react-query";
import { api, AgentEvent } from "@/lib/api";
import { fmt } from "@/lib/format";
import { AuthGate } from "@/components/ui/AuthGate";
import { SectionTitle, Muted, StatCard, Panel, Table, Skeleton } from "@/components/ui/data";

const STALE_SECONDS = 120;

function eventResult(e: AgentEvent): string {
  return e.kind === "order" ? `order ${e.status ?? ""}` : e.outcome ?? "";
}

function eventDetail(e: AgentEvent): string {
  if (e.kind === "order") {
    const w = e.target_weight == null ? "" : ` → ${fmt.pct2(e.target_weight)}`;
    return `${(e.side ?? "").toUpperCase()} ${e.symbol ?? ""}${w} · ${e.reason ?? e.rationale ?? ""}`;
  }
  return e.error ?? "";
}

function resultClass(e: AgentEvent): string {
  const r = e.kind === "order" ? e.status : e.outcome;
  if (r === "ok" || r === "submitted") return "text-gain";
  if (r === "vetoed" || r === "error" || r === "timeout" || r === "invalid") return "text-loss";
  return "";
}

function AgentsInner() {
  const q = useQuery({ queryKey: ["agents", 200], queryFn: () => api.agentActivity(200), refetchInterval: 10000 });
  const d = q.data;
  const sup = d?.supervisor;
  const supLabel = !sup ? "never started"
    : sup.status !== "running" ? sup.status
    : sup.age_seconds > STALE_SECONDS ? `stale (${Math.round(sup.age_seconds)}s)` : "running";
  const supCls = supLabel === "running" ? "text-gain" : "text-loss";
  const positions = Object.entries(d?.sleeve.positions ?? {}).map(([symbol, p]) => ({ symbol, ...p }));

  return (
    <main style={{ flex: 1, padding: 16, display: "flex", flexDirection: "column", gap: 16, maxWidth: 1600, margin: "0 auto", width: "100%" }}>
      <SectionTitle>
        Agents <Muted>· Gemini agent runtime · paper only{d ? ` · updated ${new Date(d.as_of).toLocaleTimeString()}` : ""}</Muted>
      </SectionTitle>

      {!d ? <Skeleton h={92} /> : (
        <div className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))" }}>
          <StatCard label="Supervisor" value={supLabel} cls={supCls} />
          <StatCard label="Kill switch" value={d.kill_switch ? "ENGAGED (paused)" : "off"} cls={d.kill_switch ? "text-loss" : ""} />
          <StatCard label="Spend today" value={`${fmt.usd2(d.budget.spent_usd)} / ${fmt.usd2(d.budget.daily_usd)}`}
                    cls={d.budget.spent_usd >= d.budget.daily_usd ? "text-loss" : ""} />
          <StatCard label="Model calls today" value={String(d.budget.calls ?? 0)} />
          <StatCard label="Agent orders today" value={String(d.sleeve.day?.orders ?? 0)} />
          <StatCard label="Sleeve positions" value={String(positions.length)} />
        </div>
      )}

      <Panel title="Agent sleeve" note="positions agents opened on the paper account (risk-checked)">
        <Table
          cols={["symbol", "qty", "entry", "stop", "price"]}
          rows={positions}
          fmt={(row, c) => (c === "symbol" ? row.symbol : c === "qty" ? fmt.ratio(row.qty, 4) : fmt.usd2(row[c]))}
          empty="Agents hold no positions"
        />
      </Panel>

      <Panel title="Activity" note="model calls and order attempts, newest first">
        <Table
          cols={["ts", "agent", "result", "model", "cost", "detail"]}
          rows={d?.events ?? []}
          fmt={(row, c) => {
            const e = row as AgentEvent;
            if (c === "ts") return String(e.ts ?? "").replace("T", " ").slice(0, 19);
            if (c === "result") return eventResult(e);
            if (c === "cost") return e.cost_usd == null ? "—" : `$${e.cost_usd.toFixed(4)}`;
            if (c === "detail") return eventDetail(e);
            return String((row as Record<string, unknown>)[c] ?? "");
          }}
          cls={(row, c) => (c === "result" ? resultClass(row as AgentEvent) : "")}
          empty="No agent activity yet"
        />
      </Panel>
    </main>
  );
}

export default function AgentsPage() {
  return <AuthGate><AgentsInner /></AuthGate>;
}
