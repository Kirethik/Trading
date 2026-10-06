"use client";
/* Shared bits for the agent pages: avatar, status light, relative time, tone colours. */
import type { AgentStats, Tone } from "@/lib/api";

export const TONE_LABEL: Record<Tone, string> = { good: "went well", bad: "problem", neutral: "note" };

export const TONE_COLOR: Record<Tone, string> = {
  good: "var(--gain)",
  bad: "var(--loss)",
  neutral: "var(--text-secondary)",
};

const STATUS: Record<AgentStats["status"], { label: string; color: string; pulse: boolean }> = {
  active: { label: "Working", color: "var(--gain)", pulse: true },
  resting: { label: "Resting", color: "var(--text-secondary)", pulse: false },
  paused: { label: "Paused", color: "var(--warn)", pulse: false },
  "not started": { label: "Not started yet", color: "var(--text-muted)", pulse: false },
};

export function timeAgo(ts: string | null | undefined): string {
  if (!ts) return "never";
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return "unknown time";
  const secs = Math.max(0, (Date.now() - t) / 1000);
  if (secs < 60) return "just now";
  const mins = secs / 60;
  if (mins < 60) return `${Math.round(mins)} min ago`;
  const hours = mins / 60;
  if (hours < 24) return `${Math.round(hours)} h ago`;
  const days = Math.round(hours / 24);
  if (days >= 30) return new Date(ts).toLocaleDateString();
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

export function StatusLight({ status }: { status: AgentStats["status"] }) {
  const s = STATUS[status];
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, color: s.color }}>
      <span className={s.pulse ? "agent-pulse" : ""}
            style={{ width: 8, height: 8, borderRadius: 99, background: s.color, display: "inline-block" }} />
      {s.label}
    </span>
  );
}

export function AgentAvatar({ icon, color, size = 56 }: { icon: string; color: string; size?: number }) {
  return (
    <div aria-hidden style={{
      width: size, height: size, borderRadius: "50%", flexShrink: 0,
      display: "grid", placeItems: "center", fontSize: size * 0.46,
      background: `radial-gradient(circle at 30% 25%, ${color}55, ${color}14 70%)`,
      border: `1px solid ${color}88`,
      boxShadow: `0 0 0 4px ${color}14, 0 8px 24px -8px ${color}66`,
    }}>
      {icon}
    </div>
  );
}

export function ConfidenceBadge({ level }: { level: string | null | undefined }) {
  const map: Record<string, [string, string]> = {
    high: ["very sure", "var(--gain)"], medium: ["fairly sure", "var(--warn)"], low: ["not sure yet", "var(--text-secondary)"],
  };
  const [label, color] = map[level ?? ""] ?? ["unknown", "var(--text-muted)"];
  return (
    <span style={{ fontSize: 11, padding: "2px 8px", borderRadius: 99, color, border: `1px solid ${color}55`, background: `${color}12` }}>
      {label}
    </span>
  );
}
