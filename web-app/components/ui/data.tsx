"use client";
/* Small presentational primitives shared by the Portfolio & Watchlist pages.
   Styling matches the terminal theme (CSS-var tokens + tabular numerics). */
import type { ReactNode } from "react";

export function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <div style={{
      fontSize: 12, fontWeight: 600, textTransform: "uppercase",
      letterSpacing: "0.08em", color: "var(--text-secondary)",
    }}>{children}</div>
  );
}

export function Muted({ children }: { children: ReactNode }) {
  return (
    <span style={{ color: "var(--text-muted)", fontWeight: 400, textTransform: "none", letterSpacing: 0 }}>
      {children}
    </span>
  );
}

export function Skeleton({ h }: { h: number }) {
  return <div className="skeleton" style={{ height: h, borderRadius: 8 }} />;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div style={{ fontSize: 13, color: "var(--text-muted)", padding: "12px 4px" }}>{children}</div>;
}

export function StatCard({ label, value, cls }: { label: string; value: string; cls?: string }) {
  return (
    <div className="rounded-lg p-4" style={{ background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
      <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-muted)" }}>
        {label}
      </div>
      <div className={`tabnum ${cls ?? ""}`} style={{ fontSize: 22, fontWeight: 700, marginTop: 4 }}>
        {value}
      </div>
    </div>
  );
}

export function Panel({ title, note, children }: { title: string; note?: string; children: ReactNode }) {
  return (
    <div className="rounded-lg p-4" style={{ background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
      <div className="flex items-baseline gap-2" style={{ marginBottom: 10 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-secondary)" }}>{title}</span>
        {note ? <span style={{ fontSize: 11, color: "var(--text-muted)" }}>— {note}</span> : null}
      </div>
      {children}
    </div>
  );
}

/* eslint-disable @typescript-eslint/no-explicit-any */
export function Table({ cols, rows, fmt, cls, empty }: {
  cols: string[];
  rows: any[];
  fmt?: (row: any, col: string) => string;
  cls?: (row: any, col: string) => string;
  empty?: string;
}) {
  if (!rows.length) {
    return <Empty>{empty ?? "No data"}</Empty>;
  }
  const numeric = (c: string) => rows.some((r) => typeof r[c] === "number");
  return (
    <div style={{ overflowX: "auto" }}>
      <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c} style={{
                textAlign: numeric(c) ? "right" : "left", padding: "6px 10px",
                color: "var(--text-muted)", fontWeight: 500, textTransform: "capitalize",
                borderBottom: "1px solid var(--border)", whiteSpace: "nowrap",
              }}>{c.replace(/_/g, " ")}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ borderBottom: "1px solid var(--border-soft)" }}>
              {cols.map((c) => (
                <td key={c} className={`${numeric(c) ? "tabnum" : ""} ${cls ? cls(row, c) : ""}`} style={{
                  textAlign: numeric(c) ? "right" : "left", padding: "6px 10px",
                  color: "var(--text-primary)", whiteSpace: "nowrap",
                }}>{fmt ? fmt(row, c) : String(row[c] ?? "")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
