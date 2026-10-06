"use client";
import type { AgentLesson } from "@/lib/api";
import { ConfidenceBadge, timeAgo } from "./shared";

/* A lesson styled as a page torn from the agent's notebook. */
export function LessonCard({ lesson, color, index }: { lesson: AgentLesson; color: string; index: number }) {
  return (
    <article className="notebook" style={{
      ["--agent" as string]: color,
      position: "relative", borderRadius: 10, padding: "0 16px 0 28px", lineHeight: "26px",
      border: "1px solid var(--border)", transform: `rotate(${index % 2 ? 0.35 : -0.35}deg)`,
    }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center", height: 26, marginBottom: 0 }}>
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>Lesson #{index + 1} · {timeAgo(lesson.when)}</span>
        <ConfidenceBadge level={lesson.confidence} />
      </div>
      <p style={{ fontSize: 15, lineHeight: "26px", margin: 0, color: "var(--text-primary)", fontWeight: 500 }}>
        “{lesson.lesson}”
      </p>
      {lesson.evidence ? (
        <p style={{ fontSize: 12, lineHeight: "26px", color: "var(--text-secondary)", margin: "0 0 26px" }}>
          <strong style={{ color }}>Why I think so:</strong> {lesson.evidence}
        </p>
      ) : null}
    </article>
  );
}
