"use client";
/* The Watchman's views: a system-health banner and an incident log. */
import Link from "next/link";
import type { WatchEvent, WatchStatus } from "@/lib/api";
import { TONE_COLOR, timeAgo } from "./shared";

const SEVERITY: Record<string, { label: string; color: string }> = {
  critical: { label: "Serious", color: "var(--loss)" },
  warn: { label: "Heads-up", color: "var(--warn)" },
  info: { label: "Resolved", color: "var(--gain)" },
};

export function HealthBanner({ watch }: { watch: WatchStatus }) {
  const issues = watch.issues;
  const serious = issues.some((i) => i.severity === "critical");
  const color = watch.status === "not started" ? "var(--text-muted)"
    : serious ? "var(--loss)" : issues.length ? "var(--warn)" : "var(--gain)";
  const title = watch.status === "not started" ? "The Watchman hasn't started watching yet"
    : issues.length === 0 ? "All clear — nothing needs your attention"
    : `${issues.length} thing${issues.length > 1 ? "s" : ""} need${issues.length > 1 ? "" : "s"} attention`;
  return (
    <Link href="/agents/watchman" role="status" style={{
      display: "flex", gap: 14, alignItems: "center", padding: "14px 18px", borderRadius: 12,
      textDecoration: "none", color: "inherit",
      background: `linear-gradient(90deg, color-mix(in srgb, ${color} 16%, transparent), var(--bg-surface) 70%)`,
      border: `1px solid color-mix(in srgb, ${color} 45%, var(--border))`,
    }}>
      <span aria-hidden style={{ fontSize: 28 }}>🛡️</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 16, fontWeight: 700, color }}>{title}</div>
        <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 2 }}>
          {issues.length ? issues.map((i) => i.title).join(" · ") : `Last check ${timeAgo(watch.last_check)}`}
          {watch.agents_paused_until ? <> · <strong style={{ color: "var(--warn)" }}>
            Agent buying paused until {new Date(watch.agents_paused_until).toLocaleTimeString()}</strong></> : null}
        </div>
      </div>
      <span style={{ fontSize: 12, color: "var(--text-muted)", whiteSpace: "nowrap" }}>Details →</span>
    </Link>
  );
}

export function WatchLog({ events }: { events: WatchEvent[] }) {
  if (!events.length) {
    return <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>
      Nothing has gone wrong yet. When something does, you&apos;ll see what happened, what I did, and what I suggest.
    </p>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {events.map((e, i) => {
        const sev = SEVERITY[e.severity] ?? SEVERITY.warn;
        return (
          <article key={`${e.when}-${i}`} style={{
            padding: "14px 16px", borderRadius: 12, background: "var(--bg-surface)",
            border: "1px solid var(--border)", borderLeft: `4px solid ${sev.color}`,
          }}>
            <header style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ fontSize: 11, fontWeight: 700, padding: "2px 8px", borderRadius: 99, color: sev.color,
                             border: `1px solid ${sev.color}`, textTransform: "uppercase", letterSpacing: "0.05em" }}>
                {sev.label}
              </span>
              <strong style={{ fontSize: 15, color: TONE_COLOR[e.tone] === "var(--text-secondary)" ? "var(--text-primary)" : TONE_COLOR[e.tone] }}>
                {e.headline}
              </strong>
              <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--text-muted)" }}>{timeAgo(e.when)}</span>
            </header>
            {e.explanation ? <p style={{ fontSize: 14, lineHeight: 1.55, margin: "8px 0 0", color: "var(--text-secondary)" }}>{e.explanation}</p> : null}
            {e.actions.length ? (
              <div style={{ marginTop: 10 }}>
                <div style={{ fontSize: 12, fontWeight: 600, color: "var(--text-primary)", marginBottom: 4 }}>What I did</div>
                <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13, lineHeight: 1.6, listStyle: "disc" }}>
                  {e.actions.map((a, j) => <li key={j}>{a}</li>)}
                </ul>
              </div>
            ) : null}
            {e.suggestion ? (
              <p style={{ fontSize: 13, margin: "10px 0 0" }}>
                <strong style={{ color: "#0EA5E9" }}>My suggestion:</strong> {e.suggestion}
              </p>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}
