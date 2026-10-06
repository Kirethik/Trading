"use client";
import type { DiaryItem } from "@/lib/api";
import { TONE_COLOR, TONE_LABEL, timeAgo } from "./shared";

export function DiaryTimeline({ items, color }: { items: DiaryItem[]; color: string }) {
  if (!items.length) {
    return <p style={{ fontSize: 13, color: "var(--text-muted)" }}>Nothing written yet. Entries appear after the agent&apos;s first task.</p>;
  }
  return (
    <ol style={{ listStyle: "none", margin: 0, padding: 0, position: "relative" }}>
      <span aria-hidden style={{ position: "absolute", left: 5, top: 6, bottom: 6, width: 2,
                                 background: `linear-gradient(${color}66, var(--border-soft))` }} />
      {items.map((it, i) => (
        <li key={`${it.when}-${i}`} style={{ position: "relative", paddingLeft: 26, paddingBottom: 16 }}>
          <span role="img" aria-label={TONE_LABEL[it.tone]} title={TONE_LABEL[it.tone]}
                style={{ position: "absolute", left: 0, top: 5, width: 12, height: 12, borderRadius: 99,
                         background: "var(--bg-base)", border: `3px solid ${TONE_COLOR[it.tone]}` }} />
          <div style={{ fontSize: 11, color: "var(--text-muted)" }}>{timeAgo(it.when)}</div>
          <div style={{ fontSize: 14, fontWeight: 600, color: "var(--text-primary)" }}>{it.headline}</div>
          {it.detail ? <div style={{ fontSize: 13, color: "var(--text-secondary)", marginTop: 2, lineHeight: 1.5 }}>{it.detail}</div> : null}
        </li>
      ))}
    </ol>
  );
}
