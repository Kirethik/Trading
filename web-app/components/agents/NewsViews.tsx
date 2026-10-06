"use client";
import Link from "next/link";
/* The Reporter's views: what it read, what it concluded, what it did (and why). */
import type { NewsAction, NewsCard, NewsConclusion } from "@/lib/api";
import { ConfidenceBadge, TONE_COLOR, timeAgo } from "./shared";

const SOURCE_LABEL: Record<string, string> = {
  alpaca: "News wire", sec: "SEC filing", exa: "Web", feargreed: "Market mood", firecrawl: "Web",
};

function safeHref(url: string): string | undefined {
  return /^https?:\/\//i.test(url) ? url : undefined;
}

export function SourceBadge({ card }: { card: NewsCard }) {
  return (
    <span style={{ fontSize: 10, fontWeight: 700, padding: "2px 7px", borderRadius: 99, letterSpacing: "0.04em",
                   textTransform: "uppercase", color: "var(--text-secondary)", border: "1px solid var(--border)" }}>
      {SOURCE_LABEL[card.source] ?? card.source} · {card.publisher}
    </span>
  );
}

export function ArticleLine({ card, compact = false }: { card: NewsCard; compact?: boolean }) {
  const href = safeHref(card.url);
  const title = href
    ? <a href={href} target="_blank" rel="noopener noreferrer" style={{ color: "var(--text-primary)" }}>{card.headline}</a>
    : <span>{card.headline}</span>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <SourceBadge card={card} />
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>{timeAgo(card.when)}</span>
      </div>
      <div style={{ fontSize: compact ? 13 : 14, fontWeight: 600, lineHeight: 1.4 }}>{title}</div>
      {!compact && card.summary ? <div style={{ fontSize: 13, color: "var(--text-secondary)", lineHeight: 1.5 }}>{card.summary}</div> : null}
    </div>
  );
}

/* -2 .. +2 on a red -> grey -> green track with a marker */
function MoodGauge({ value }: { value: number }) {
  const pct = ((Math.max(-2, Math.min(2, value)) + 2) / 4) * 100;
  return (
    <div aria-label={`sentiment ${value.toFixed(1)} of plus or minus 2`} style={{ position: "relative", height: 8, borderRadius: 99,
         background: "linear-gradient(90deg, var(--loss), var(--bg-elevated) 50%, var(--gain))" }}>
      <span style={{ position: "absolute", left: `calc(${pct}% - 7px)`, top: -4, width: 14, height: 14, borderRadius: 99,
                     background: "var(--text-primary)", border: "3px solid var(--bg-base)" }} />
    </div>
  );
}

export function Conclusions({ items }: { items: NewsConclusion[] }) {
  if (!items.length) {
    return <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>No readings yet — they appear after my first news run.</p>;
  }
  return (
    <div style={{ display: "grid", gap: 14, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 320px), 1fr))" }}>
      {items.map((c) => (
        <article key={`${c.symbol}-${c.when}`} style={{ padding: 16, borderRadius: 12, background: "var(--bg-surface)",
                                                       border: "1px solid var(--border)", display: "flex", flexDirection: "column", gap: 10 }}>
          <header style={{ display: "flex", alignItems: "baseline", gap: 10, flexWrap: "wrap" }}>
            <strong className="mono" style={{ fontSize: 18 }}>{c.symbol}</strong>
            <span style={{ fontSize: 14, fontWeight: 700, color: TONE_COLOR[c.tone] }}>{c.mood}</span>
            <ConfidenceBadge level={c.confidence} />
            <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-muted)" }}>{timeAgo(c.when)}</span>
          </header>
          <MoodGauge value={c.sentiment} />
          <p style={{ fontSize: 14, margin: 0, lineHeight: 1.55 }}>{c.why}</p>
          {c.supporting.length ? (
            <div style={{ display: "flex", flexDirection: "column", gap: 8, borderTop: "1px solid var(--border-soft)", paddingTop: 10 }}>
              <span style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Based on</span>
              {c.supporting.map((a) => <ArticleLine key={a.id} card={a} compact />)}
            </div>
          ) : null}
        </article>
      ))}
    </div>
  );
}

export function ActionTimeline({ items }: { items: NewsAction[] }) {
  if (!items.length) {
    return <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>
      I haven&apos;t acted on any news yet. I only act on strongly good or bad news, and buying needs two independent sources.
    </p>;
  }
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
      {items.map((a, i) => (
        <article key={`${a.when}-${a.symbol}-${i}`} style={{ padding: "14px 16px", borderRadius: 12, background: "var(--bg-surface)",
                                                             border: "1px solid var(--border)", borderLeft: `4px solid ${TONE_COLOR[a.tone]}` }}>
          <header style={{ display: "flex", gap: 8, alignItems: "baseline", flexWrap: "wrap" }}>
            <strong style={{ fontSize: 15, color: TONE_COLOR[a.tone] === "var(--text-secondary)" ? "var(--text-primary)" : TONE_COLOR[a.tone] }}>
              {a.headline}
            </strong>
            {a.result ? <span style={{ fontSize: 12, color: "var(--text-muted)" }}>· outcome: {a.result}</span> : null}
            <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--text-muted)" }}>{timeAgo(a.when)}</span>
          </header>
          <p style={{ fontSize: 14, margin: "8px 0 0", lineHeight: 1.55 }}>
            <strong style={{ color: "#EC4899" }}>Why:</strong> {a.reason}
          </p>
          {a.risk_check ? (
            <p style={{ fontSize: 13, margin: "6px 0 0", color: "var(--text-secondary)" }}>
              <strong>Safety check said:</strong> {a.risk_check}
            </p>
          ) : null}
          {a.supporting.length ? (
            <div style={{ marginTop: 10, display: "flex", flexDirection: "column", gap: 8 }}>
              <span style={{ fontSize: 11, color: "var(--text-muted)", textTransform: "uppercase", letterSpacing: "0.06em" }}>Supporting news</span>
              {a.supporting.map((c) => <ArticleLine key={c.id} card={c} compact />)}
            </div>
          ) : null}
        </article>
      ))}
    </div>
  );
}

export function ReadingList({ items }: { items: NewsCard[] }) {
  if (!items.length) return <p style={{ fontSize: 13, color: "var(--text-muted)", margin: 0 }}>Nothing read yet.</p>;
  return (
    <div style={{ display: "grid", gap: 12, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 300px), 1fr))" }}>
      {items.map((c) => (
        <div key={c.id} style={{ padding: 14, borderRadius: 10, background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
          <ArticleLine card={c} />
          <div style={{ marginTop: 6, fontSize: 11, color: "var(--text-muted)" }}>About: {c.symbols.join(", ")}</div>
        </div>
      ))}
    </div>
  );
}

export function NewsStrip({ items }: { items: NewsAction[] }) {
  if (!items.length) return null;
  return (
    <section style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      <h2 style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>📰 News &amp; actions</h2>
      <div style={{ display: "grid", gap: 10, gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 300px), 1fr))" }}>
        {items.map((a, i) => (
          <Link key={`${a.when}-${i}`} href="/agents/reporter" style={{ textDecoration: "none", color: "inherit", padding: "12px 14px",
             borderRadius: 10, background: "var(--bg-surface)", border: "1px solid var(--border)", borderLeft: `4px solid ${TONE_COLOR[a.tone]}` }}>
            <div style={{ fontSize: 14, fontWeight: 700 }}>{a.headline}</div>
            <div style={{ fontSize: 12, color: "var(--text-secondary)", marginTop: 4, lineHeight: 1.45 }}>{a.reason}</div>
            {a.supporting[0] ? <div style={{ fontSize: 12, color: "var(--text-muted)", marginTop: 6 }}>📎 {a.supporting[0].headline}</div> : null}
          </Link>
        ))}
      </div>
    </section>
  );
}
