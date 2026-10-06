"use client";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import { AuthGate } from "@/components/ui/AuthGate";
import { Skeleton } from "@/components/ui/data";
import { AgentAvatar, StatusLight, timeAgo } from "@/components/agents/shared";
import { DiaryTimeline } from "@/components/agents/DiaryTimeline";
import { LessonCard } from "@/components/agents/LessonCard";
import { DebateThread } from "@/components/agents/DebateThread";
import { HealthBanner, WatchLog } from "@/components/agents/WatchViews";
import { ActionTimeline, Conclusions, ReadingList } from "@/components/agents/NewsViews";

function Section({ title, hint, children }: { title: string; hint: string; children: ReactNode }) {
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      <div>
        <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>{title}</h2>
        <p style={{ fontSize: 13, color: "var(--text-muted)", margin: "2px 0 0" }}>{hint}</p>
      </div>
      {children}
    </section>
  );
}

function Chips({ items, color, empty }: { items: { name: string; times: number }[]; color: string; empty: string }) {
  if (!items.length) return <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>{empty}</p>;
  const max = Math.max(...items.map((i) => i.times));
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
      {items.map((i) => (
        <span key={i.name} style={{
          display: "inline-flex", gap: 8, alignItems: "center", padding: "6px 12px", borderRadius: 99,
          fontSize: 13, border: `1px solid ${color}55`,
          background: `color-mix(in srgb, ${color} ${Math.round(8 + 22 * (i.times / max))}%, transparent)`,
        }}>
          {i.name}<span className="tabnum" style={{ fontSize: 11, color: "var(--text-secondary)" }}>×{i.times}</span>
        </span>
      ))}
    </div>
  );
}

function ProfileInner() {
  const { name } = useParams<{ name: string }>();
  const q = useQuery({
    queryKey: ["agent", name], queryFn: () => api.agent(name), refetchInterval: 15000,
    retry: (count, err) => !/no such agent/i.test((err as Error).message) && count < 3,
  });
  const p = q.data;

  if (q.isError) {
    return (
      <main style={{ padding: 24 }}>
        <p style={{ color: "var(--loss)" }}>Couldn&apos;t find this agent ({(q.error as Error).message}).</p>
        <Link href="/agents" style={{ color: "var(--accent)" }}>← Back to the team</Link>
      </main>
    );
  }
  if (!p) return <main style={{ padding: 16, maxWidth: 1100, margin: "0 auto", width: "100%" }}><Skeleton h={420} /></main>;

  const s = p.stats;
  const success = s.success_rate == null ? "—" : `${Math.round(s.success_rate * 100)}%`;
  return (
    <main style={{ flex: 1, padding: 16, display: "flex", flexDirection: "column", gap: 28, maxWidth: 1100, margin: "0 auto", width: "100%" }}>
      <Link href="/agents" style={{ fontSize: 13, color: "var(--text-secondary)", textDecoration: "none" }}>← The team</Link>

      {/* ---- who I am ---- */}
      <header style={{
        display: "flex", gap: 20, alignItems: "center", flexWrap: "wrap", padding: 24, borderRadius: 16,
        background: `radial-gradient(120% 140% at 0% 0%, ${p.color}22, var(--bg-surface) 55%)`,
        border: `1px solid ${p.color}44`,
      }}>
        <AgentAvatar icon={p.icon} color={p.color} size={88} />
        <div style={{ flex: "1 1 320px", minWidth: 0 }}>
          <div style={{ display: "flex", gap: 12, alignItems: "baseline", flexWrap: "wrap" }}>
            <h1 style={{ fontSize: 30, fontWeight: 800, margin: 0, letterSpacing: "-0.02em" }}>{p.display_name}</h1>
            <StatusLight status={s.status} />
          </div>
          <p style={{ fontSize: 17, margin: "6px 0 0", color: "var(--text-primary)", fontStyle: "italic" }}>“{p.voice}”</p>
          <p style={{ fontSize: 14, margin: "10px 0 0", color: "var(--text-secondary)", lineHeight: 1.6, maxWidth: 680 }}>{p.about}</p>
        </div>
      </header>

      <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(160px, 1fr))" }}>
        {[["Tasks this week", String(s.tasks_this_week)], ["Answered well", success],
          ["Lessons learned", String(s.lessons)], ["Last active", timeAgo(s.last_active)]].map(([label, value]) => (
          <div key={label} style={{ padding: "12px 14px", borderRadius: 10, background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
            <div className="tabnum" style={{ fontSize: 22, fontWeight: 700 }}>{value}</div>
            <div style={{ fontSize: 12, color: "var(--text-muted)" }}>{label}</div>
          </div>
        ))}
      </div>

      {p.watch ? <HealthBanner watch={p.watch} /> : null}

      {p.watch_log ? (
        <Section title="What I've seen" hint="Every problem I spotted, what I did about it on my own, and what I suggest — newest first.">
          <WatchLog events={p.watch_log} />
        </Section>
      ) : null}

      {p.news ? (
        <>
          <Section title="What I did about it" hint="Every action I took because of the news — with my reasoning, the safety check's answer, and the articles behind it.">
            <ActionTimeline items={p.news.actions} />
          </Section>
          <Section title="What I concluded" hint="My latest reading for each stock, from very worried to very upbeat, and what it was based on.">
            <Conclusions items={p.news.conclusions} />
          </Section>
          <Section title="What I'm reading" hint="The newest articles, filings and market-mood readings I collected.">
            <ReadingList items={p.news.reading} />
          </Section>
          <Section title="My diary" hint="Everything I did, newest first, in plain words.">
            <DiaryTimeline items={p.diary} color={p.color} />
          </Section>
        </>
      ) : p.watch_log ? (
        <Section title="My diary" hint="Everything I did, newest first, in plain words.">
          <DiaryTimeline items={p.diary} color={p.color} />
        </Section>
      ) : <>
      <Section title="What I've been looking at" hint="The strategies and numbers I studied most recently — bigger glow means more often.">
        <Chips items={p.looked_at.strategies} color={p.color} empty="I haven't studied any strategies yet." />
        <Chips items={p.looked_at.numbers} color="#8891A0" empty="" />
      </Section>

      <Section title="What I've learned" hint="Every few days I compare my past calls with what actually happened, and write myself notes.">
        {p.lessons.length ? (
          <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
            {p.lessons.map((l, i) => <LessonCard key={`${l.when}-${i}`} lesson={l} color={p.color} index={i} />)}
          </div>
        ) : <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>
              No lessons yet — I need a few days of decisions before I can look back and learn from them.
            </p>}
      </Section>

      <div style={{ display: "grid", gap: 28, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 420px), 1fr))", alignItems: "start" }}>
        <Section title="My diary" hint="Everything I did, newest first, in plain words.">
          <DiaryTimeline items={p.diary} color={p.color} />
        </Section>
        <Section title="Debates I took part in" hint="When a strategy only just passes, the Tuner argues for it, the Skeptic against, and the Judge decides.">
          {p.debates.length ? p.debates.slice(0, 6).map((d, i) => <DebateThread key={`${d.when}-${i}`} debate={d} />)
            : <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>No debates yet.</p>}
        </Section>
      </div>
      </>}
    </main>
  );
}

export default function AgentProfilePage() {
  return <AuthGate><ProfileInner /></AuthGate>;
}
