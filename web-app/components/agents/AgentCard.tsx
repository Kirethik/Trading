"use client";
import Link from "next/link";
import type { AgentTeamPayload } from "@/lib/api";
import { AgentAvatar, StatusLight, TONE_COLOR, timeAgo } from "./shared";

type Agent = AgentTeamPayload["agents"][number];

export function AgentCard({ agent }: { agent: Agent }) {
  const s = agent.stats;
  const success = s.success_rate == null ? "—" : `${Math.round(s.success_rate * 100)}%`;
  return (
    <Link href={`/agents/${agent.name}`} className="agent-card" style={{
      ["--agent" as string]: agent.color,
      display: "flex", flexDirection: "column", gap: 14, padding: 20, borderRadius: 14,
      background: `linear-gradient(160deg, ${agent.color}10, var(--bg-surface) 45%)`,
      border: "1px solid var(--border)", textDecoration: "none", color: "inherit",
    }}>
      <div style={{ display: "flex", gap: 14, alignItems: "center" }}>
        <AgentAvatar icon={agent.icon} color={agent.color} />
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 18, fontWeight: 700 }}>{agent.display_name}</div>
          <StatusLight status={s.status} />
        </div>
      </div>
      <p style={{ fontSize: 14, color: "var(--text-secondary)", margin: 0, lineHeight: 1.5 }}>{agent.title}</p>

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, 1fr)", gap: 8 }}>
        {[["Tasks this week", String(s.tasks_this_week)], ["Answered well", success], ["Lessons learned", String(s.lessons)]]
          .map(([label, value]) => (
            <div key={label} style={{ background: "var(--bg-elevated)", borderRadius: 8, padding: "8px 10px" }}>
              <div className="tabnum" style={{ fontSize: 18, fontWeight: 700 }}>{value}</div>
              <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{label}</div>
            </div>
          ))}
      </div>

      <div style={{ fontSize: 13, borderTop: "1px solid var(--border-soft)", paddingTop: 12, minHeight: 40 }}>
        {agent.latest ? (
          <>
            <span style={{ color: "var(--text-muted)" }}>Latest · {timeAgo(agent.latest.when)} — </span>
            <span style={{ color: TONE_COLOR[agent.latest.tone] }}>{agent.latest.headline}</span>
          </>
        ) : <span style={{ color: "var(--text-muted)" }}>Hasn&apos;t done anything yet.</span>}
      </div>
      <span style={{ fontSize: 12, color: agent.color, fontWeight: 600 }}>Open {agent.display_name}&apos;s page →</span>
    </Link>
  );
}
