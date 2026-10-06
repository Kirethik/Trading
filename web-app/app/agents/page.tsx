"use client";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type AgentEvent, type AgentTeamPayload, type ProviderUsage } from "@/lib/api";
import { fmt } from "@/lib/format";
import { AuthGate } from "@/components/ui/AuthGate";
import { Skeleton, Table } from "@/components/ui/data";
import { AgentCard } from "@/components/agents/AgentCard";
import { DebateThread } from "@/components/agents/DebateThread";
import { HealthBanner } from "@/components/agents/WatchViews";
import { NewsStrip } from "@/components/agents/NewsViews";
import { timeAgo } from "@/components/agents/shared";

const STALE_SECONDS = 120;

function TeamStrip({ team }: { team: AgentTeamPayload["team"] }) {
  const sup = team.supervisor;
  const supText = !sup ? "not started" : sup.status !== "running" ? sup.status
    : sup.age_seconds > STALE_SECONDS ? "not responding" : "running";
  const spentPct = team.budget.daily_usd ? Math.min(1, team.budget.spent_usd / team.budget.daily_usd) : 0;
  const items: [string, string, string?][] = [
    ["Spent today", `${fmt.usd2(team.budget.spent_usd)} of ${fmt.usd2(team.budget.daily_usd)} limit`],
    ["Always-on helper", supText, supText === "running" ? "var(--gain)" : "var(--text-secondary)"],
    ["Pause switch", team.kill_switch ? "ON — all agents paused" : "off", team.kill_switch ? "var(--warn)" : undefined],
    ["Agents' own trades", `${Object.keys(team.sleeve.positions).length} holdings · practice money only`],
  ];
  return (
    <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))" }}>
      {items.map(([label, value, color]) => (
        <div key={label} style={{ padding: "12px 14px", borderRadius: 10, background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
          <div style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</div>
          <div style={{ fontSize: 14, fontWeight: 600, marginTop: 4, color: color ?? "var(--text-primary)" }}>{value}</div>
          {label === "Spent today" ? (
            <div style={{ height: 4, borderRadius: 99, background: "var(--bg-elevated)", marginTop: 8 }}>
              <div style={{ width: `${spentPct * 100}%`, height: "100%", borderRadius: 99,
                            background: spentPct >= 1 ? "var(--loss)" : "var(--accent)" }} />
            </div>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function ProviderStrip({ items }: { items: ProviderUsage[] }) {
  if (!items.length) return null;
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <div>
        <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>Free AI allowance today</h2>
        <p style={{ fontSize: 13, color: "var(--text-muted)", margin: "2px 0 0" }}>
          Every AI helper runs on free plans. When one runs out, the next takes over; if all are out, the normal rules decide.
        </p>
      </div>
      <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 220px), 1fr))" }}>
        {items.map((p) => {
          const pct = p.cap ? Math.min(1, p.used / p.cap) : 0;
          const resting = Boolean(p.resting_until);
          const color = resting ? "var(--warn)" : pct >= 0.9 ? "var(--loss)" : "var(--accent)";
          return (
            <div key={p.label} style={{ padding: "10px 12px", borderRadius: 10, background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
              <div style={{ fontSize: 13, fontWeight: 600 }}>{p.label}</div>
              <div className="tabnum" style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 2 }}>
                {p.used} of {p.cap} used{resting ? ` · resting until ${new Date(p.resting_until as string).toLocaleTimeString()}` : ""}
              </div>
              <div style={{ height: 4, borderRadius: 99, background: "var(--bg-elevated)", marginTop: 8 }}>
                <div style={{ width: `${pct * 100}%`, height: "100%", borderRadius: 99, background: color }} />
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function TechnicalLog() {
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["agents-activity"], queryFn: () => api.agentActivity(200),
                       enabled: open, refetchInterval: open ? 15000 : false });
  const rows = q.data?.events ?? [];
  const positions = Object.entries(q.data?.sleeve.positions ?? {}).map(([symbol, p]) => ({ symbol, ...p }));
  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
             style={{ borderRadius: 10, border: "1px solid var(--border)", background: "var(--bg-surface)", padding: "10px 14px" }}>
      <summary style={{ cursor: "pointer", fontSize: 14, fontWeight: 600 }}>
        Technical log <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>— every model call and trade attempt, unedited</span>
      </summary>
      <div style={{ display: "flex", flexDirection: "column", gap: 14, marginTop: 12 }}>
        <Table
          cols={["symbol", "qty", "entry", "stop", "price"]}
          rows={positions}
          fmt={(row, c) => (c === "symbol" ? row.symbol : c === "qty" ? fmt.ratio(row.qty, 4) : fmt.usd2(row[c]))}
          empty="The agents hold no positions"
        />
        <Table
          cols={["ts", "agent", "result", "model", "cost", "detail"]}
          rows={rows}
          fmt={(row, c) => {
            const e = row as AgentEvent;
            if (c === "ts") return String(e.ts ?? "").replace("T", " ").slice(0, 19);
            if (c === "result") return e.kind === "order" ? `order ${e.status ?? ""}` : e.outcome ?? "";
            if (c === "cost") return e.cost_usd == null ? "—" : `$${e.cost_usd.toFixed(4)}`;
            if (c === "detail") return e.kind === "order"
              ? `${(e.side ?? "").toUpperCase()} ${e.symbol ?? ""} · ${e.reason ?? e.rationale ?? ""}` : e.error ?? "";
            return String((row as Record<string, unknown>)[c] ?? "");
          }}
          empty={q.isLoading ? "Loading…" : "No agent activity yet"}
        />
      </div>
    </details>
  );
}

function useTeam() {
  return useQuery({ queryKey: ["agents-team"], queryFn: api.agents, refetchInterval: 15000 });
}

function TeamInner() {
  const q = useTeam();
  const d = q.data;
  return (
    <main style={{ flex: 1, padding: 16, display: "flex", flexDirection: "column", gap: 24, maxWidth: 1280, margin: "0 auto", width: "100%" }}>
      <header>
        <h1 style={{ fontSize: 28, fontWeight: 800, margin: 0, letterSpacing: "-0.02em" }}>Meet the team</h1>
        <p style={{ fontSize: 15, color: "var(--text-secondary)", margin: "6px 0 0", maxWidth: 720, lineHeight: 1.55 }}>
          Five AI helpers look after your strategies. Three of them argue with each other before big
          decisions and look back at their own calls to learn; the Watchman keeps an eye on everything
          while it runs; and the Reporter reads the news and acts only when several sources agree.
          They only ever use practice money, and every trade still goes through your safety checks.
          {d ? <span style={{ color: "var(--text-muted)" }}> · updated {timeAgo(d.as_of)}</span> : null}
        </p>
      </header>

      {q.isError ? <p style={{ color: "var(--loss)" }}>Couldn&apos;t load the team: {(q.error as Error).message}</p> : null}

      {!d ? <Skeleton h={260} /> : (
        <>
          <HealthBanner watch={d.watch} />
          <div style={{ display: "grid", gap: 16, gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
            {d.agents.map((a) => <AgentCard key={a.name} agent={a} />)}
          </div>
          <TeamStrip team={d.team} />
          <NewsStrip items={d.news ?? []} />
          <ProviderStrip items={d.providers ?? []} />
          <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>Latest debate</h2>
            {d.latest_debate ? <DebateThread debate={d.latest_debate} /> : (
              <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>
                No debates yet. The team debates only when a strategy passes every safety test, but only just.
              </p>
            )}
          </section>
          <TechnicalLog />
        </>
      )}
    </main>
  );
}

export default function AgentsPage() {
  return <AuthGate><TeamInner /></AuthGate>;
}
