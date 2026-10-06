"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Backtest" },
  { href: "/portfolio", label: "Portfolio" },
  { href: "/watchlist", label: "Watchlist" },
  { href: "/agents", label: "Agents" },
];

export function Nav() {
  const pathname = usePathname();
  return (
    <header className="flex items-center justify-between px-5 py-3" style={{
      background: "var(--bg-surface)", borderBottom: "1px solid var(--border)",
      position: "sticky", top: 0, zIndex: 50,
    }}>
      <div className="flex items-center gap-4">
        <Link href="/" className="flex items-center gap-2" style={{ textDecoration: "none" }}>
          <span style={{
            fontSize: 18, fontWeight: 700, letterSpacing: "-0.01em",
            background: "linear-gradient(135deg,#6366F1,#10B981)",
            WebkitBackgroundClip: "text", WebkitTextFillColor: "transparent",
          }}>◈ Regime Terminal</span>
          <span style={{
            fontSize: 10, padding: "2px 7px", borderRadius: 99,
            background: "var(--accent-bg)", color: "var(--accent)",
            border: "1px solid rgba(99,102,241,0.25)", letterSpacing: "0.05em",
          }}>BETA</span>
        </Link>
        <nav className="flex items-center gap-1">
          {LINKS.map((l) => {
            const active = l.href === "/" ? pathname === "/" : pathname.startsWith(l.href);
            return (
              <Link key={l.href} href={l.href} style={{
                fontSize: 13, fontWeight: 500, padding: "6px 12px", borderRadius: 6,
                textDecoration: "none",
                color: active ? "var(--text-primary)" : "var(--text-secondary)",
                background: active ? "var(--bg-elevated)" : "transparent",
                border: active ? "1px solid var(--border)" : "1px solid transparent",
              }}>{l.label}</Link>
            );
          })}
        </nav>
      </div>
      <div className="flex items-center gap-4" style={{ fontSize: 12, color: "var(--text-muted)" }}>
        <span>HMM Regime Engine</span>
        <span style={{ color: "var(--gain)" }}>● Live</span>
      </div>
    </header>
  );
}
