"use client";
/* Gates live-account pages behind the same password as the legacy dashboard.
   The backtest page stays public; only Portfolio/Watchlist require a session. */
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function AuthGate({ children }: { children: React.ReactNode }) {
  const qc = useQueryClient();
  const { data: session, isLoading } = useQuery({ queryKey: ["session"], queryFn: api.session });
  const [pwd, setPwd] = useState("");
  const [err, setErr] = useState("");

  const login = useMutation({
    mutationFn: () => api.login(pwd),
    onSuccess: () => { setErr(""); setPwd(""); qc.invalidateQueries({ queryKey: ["session"] }); },
    onError: () => setErr("Incorrect password"),
  });

  if (isLoading) {
    return (
      <div style={{ maxWidth: 1600, margin: "0 auto", width: "100%", padding: 16 }}>
        <div className="skeleton" style={{ height: 120, borderRadius: 8 }} />
      </div>
    );
  }

  if (session?.authed) return <>{children}</>;

  return (
    <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", padding: 24 }}>
      <form
        onSubmit={(e) => { e.preventDefault(); login.mutate(); }}
        className="rounded-lg p-6"
        style={{ background: "var(--bg-surface)", border: "1px solid var(--border)", width: 340, textAlign: "center" }}
      >
        <h2 style={{ fontSize: 16, fontWeight: 600, margin: "0 0 4px" }}>Sign in</h2>
        <p style={{ fontSize: 12, color: "var(--text-muted)", margin: "0 0 16px" }}>
          Live portfolio data is password-protected
        </p>
        <input
          type="password"
          value={pwd}
          onChange={(e) => setPwd(e.target.value)}
          placeholder="Password"
          autoComplete="current-password"
          style={{
            width: "100%", padding: "10px 12px", borderRadius: 6, fontSize: 14,
            background: "var(--bg-elevated)", border: "1px solid var(--border)",
            color: "var(--text-primary)", marginBottom: 12,
          }}
        />
        <button type="submit" disabled={login.isPending} style={{
          width: "100%", padding: 10, borderRadius: 6, fontSize: 14, fontWeight: 600,
          background: "var(--accent)", color: "#fff", border: "none",
          cursor: login.isPending ? "default" : "pointer", opacity: login.isPending ? 0.6 : 1,
        }}>{login.isPending ? "Signing in…" : "Log in"}</button>
        {err && <div style={{ color: "var(--loss)", fontSize: 12, marginTop: 10 }}>{err}</div>}
      </form>
    </div>
  );
}
