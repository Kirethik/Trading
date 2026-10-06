"use client";
import type { Debate, DebateSide } from "@/lib/api";
import { ConfidenceBadge, TONE_COLOR, timeAgo } from "./shared";

const TUNER = { name: "The Tuner", icon: "🔧", color: "#10B981" };
const SKEPTIC = { name: "The Skeptic", icon: "🔍", color: "#F59E0B" };
const JUDGE = { name: "The Judge", icon: "⚖️", color: "#6366F1" };

function Bubble({ who, side, align }: { who: typeof TUNER; side: DebateSide | null; align: "left" | "right" }) {
  const right = align === "right";
  return (
    <div style={{ display: "flex", flexDirection: right ? "row-reverse" : "row", gap: 10, alignItems: "flex-start" }}>
      <span aria-hidden style={{ fontSize: 20, lineHeight: "32px" }}>{who.icon}</span>
      <div style={{
        maxWidth: "min(520px, 85%)", padding: "10px 14px", borderRadius: 14,
        borderTopLeftRadius: right ? 14 : 4, borderTopRightRadius: right ? 4 : 14,
        background: `${who.color}14`, border: `1px solid ${who.color}44`,
      }}>
        <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6, flexWrap: "wrap" }}>
          <strong style={{ fontSize: 13, color: who.color }}>{who.name}</strong>
          {side ? <>
            <span style={{ fontSize: 12, color: "var(--text-secondary)" }}>
              argues {side.stance === "for" ? "FOR funding" : "AGAINST funding"}
            </span>
            <ConfidenceBadge level={side.confidence} />
          </> : null}
        </div>
        {side ? (
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.55, color: "var(--text-primary)" }}>
            {side.points.map((p, i) => <li key={i}>{p}</li>)}
          </ul>
        ) : <span style={{ fontSize: 13, color: "var(--text-muted)" }}>Couldn&apos;t take part this time.</span>}
      </div>
    </div>
  );
}

export function DebateThread({ debate }: { debate: Debate }) {
  const v = debate.verdict;
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 12, padding: 16, borderRadius: 12,
                      background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
      <header style={{ display: "flex", justifyContent: "space-between", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontSize: 14, fontWeight: 700 }}>Should we fund <span className="mono">{debate.strategy}</span>?</span>
        <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{timeAgo(debate.when)}</span>
      </header>
      <Bubble who={TUNER} side={debate.tuner} align="left" />
      <Bubble who={SKEPTIC} side={debate.skeptic} align="right" />
      <div style={{
        display: "flex", gap: 10, alignItems: "center", padding: "10px 14px", borderRadius: 10,
        background: `${JUDGE.color}14`, borderLeft: `4px solid ${TONE_COLOR[v.tone]}`,
      }}>
        <span aria-hidden style={{ fontSize: 20 }}>{JUDGE.icon}</span>
        <div>
          <div style={{ fontSize: 13 }}>
            <strong style={{ color: JUDGE.color }}>The Judge decided:</strong>{" "}
            <strong style={{ color: TONE_COLOR[v.tone] }}>{v.words}</strong>
          </div>
          {v.rationale ? <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 2 }}>{v.rationale}</div> : null}
        </div>
      </div>
    </section>
  );
}
