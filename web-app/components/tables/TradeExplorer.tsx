"use client";
import { useState, useMemo } from "react";
import {
  useReactTable, getCoreRowModel, getSortedRowModel,
  getFilteredRowModel, flexRender,
  type ColumnDef, type SortingState,
} from "@tanstack/react-table";
import type { BacktestPayload } from "@/lib/api";
import { fmt, dirClass } from "@/lib/format";

interface TradeRow {
  entry_ts: string;
  exit_ts?: string;
  symbol: string;
  side: string;
  entry_price: number;
  exit_price?: number;
  realized_pnl?: number;
  pct_return?: number;
  duration_bars?: number;
  reason?: string;
}

function parseTradesFromSignals(signals: BacktestPayload["signals"]): TradeRow[] {
  const rows: TradeRow[] = [];
  let open: (typeof signals)[0] | null = null;
  for (const s of signals) {
    if (s.side === "buy") { open = s; continue; }
    if (s.side === "sell" && open) {
      const pnl = s.price - open.price;
      const ret = pnl / open.price;
      rows.push({
        entry_ts: new Date(open.ts).toISOString().slice(0, 10),
        exit_ts: new Date(s.ts).toISOString().slice(0, 10),
        symbol: "—",
        side: "Long",
        entry_price: open.price,
        exit_price: s.price,
        realized_pnl: pnl,
        pct_return: ret,
        reason: s.reason,
      });
      open = null;
    }
  }
  return rows;
}

const COLUMNS: ColumnDef<TradeRow>[] = [
  { accessorKey: "entry_ts",    header: "Entry",      size: 90 },
  { accessorKey: "exit_ts",     header: "Exit",       size: 90,  cell: (i) => i.getValue() ?? "open" },
  { accessorKey: "side",        header: "Dir",        size: 55 },
  {
    accessorKey: "entry_price",
    header: "Entry $",
    size: 90,
    cell: (i) => <span className="tabnum">{fmt.usd2(i.getValue() as number)}</span>,
  },
  {
    accessorKey: "exit_price",
    header: "Exit $",
    size: 90,
    cell: (i) => <span className="tabnum">{i.getValue() != null ? fmt.usd2(i.getValue() as number) : "—"}</span>,
  },
  {
    accessorKey: "realized_pnl",
    header: "P&L",
    size: 90,
    cell: (i) => {
      const v = i.getValue() as number | undefined;
      return <span className={`tabnum ${dirClass(v)}`}>{v != null ? fmt.usd2(v) : "—"}</span>;
    },
  },
  {
    accessorKey: "pct_return",
    header: "Return",
    size: 80,
    cell: (i) => {
      const v = i.getValue() as number | undefined;
      return <span className={`tabnum ${dirClass(v)}`}>{fmt.sign(v ?? null)}</span>;
    },
  },
  { accessorKey: "reason", header: "Reason", size: 100, cell: (i) => <span style={{ color: "var(--text-muted)" }}>{String(i.getValue() ?? "—")}</span> },
];

export function TradeExplorer({ data }: { data: BacktestPayload | null }) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const [filter, setFilter] = useState("");

  const trades = useMemo<TradeRow[]>(() => {
    if (!data) return [];
    return parseTradesFromSignals(data.signals);
  }, [data]);

  const table = useReactTable({
    data: trades,
    columns: COLUMNS,
    state: { sorting, globalFilter: filter },
    onSortingChange: setSorting,
    onGlobalFilterChange: setFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
  });

  return (
    <div className="rounded-lg overflow-hidden" style={{ background: "var(--bg-surface)", border: "1px solid var(--border)" }}>
      <div className="flex items-center justify-between px-4 py-3" style={{ borderBottom: "1px solid var(--border-soft)" }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--text-secondary)" }}>
          Trades <span style={{ color: "var(--text-muted)", fontWeight: 400 }}>({trades.length})</span>
        </span>
        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter…"
          style={{
            background: "var(--bg-elevated)", border: "1px solid var(--border)",
            color: "var(--text-primary)", borderRadius: 5, padding: "4px 10px",
            fontSize: 12, outline: "none", width: 140,
          }}
        />
      </div>

      <div style={{ overflowX: "auto", maxHeight: 320, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12 }}>
          <thead>
            {table.getHeaderGroups().map((hg) => (
              <tr key={hg.id} style={{ borderBottom: "1px solid var(--border-soft)" }}>
                {hg.headers.map((h) => (
                  <th
                    key={h.id}
                    style={{
                      padding: "8px 12px", textAlign: "left",
                      color: "var(--text-muted)", fontWeight: 500,
                      cursor: h.column.getCanSort() ? "pointer" : "default",
                      userSelect: "none", position: "sticky", top: 0,
                      background: "var(--bg-surface)", whiteSpace: "nowrap",
                    }}
                    onClick={h.column.getToggleSortingHandler()}
                  >
                    {flexRender(h.column.columnDef.header, h.getContext())}
                    {h.column.getIsSorted() === "asc" ? " ↑" : h.column.getIsSorted() === "desc" ? " ↓" : ""}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.length === 0 ? (
              <tr>
                <td colSpan={COLUMNS.length} style={{ padding: "24px", textAlign: "center", color: "var(--text-muted)" }}>
                  {data ? "No trades in this period" : "Run a backtest to see trades"}
                </td>
              </tr>
            ) : (
              table.getRowModel().rows.map((row, i) => (
                <tr
                  key={row.id}
                  style={{
                    background: i % 2 === 0 ? "transparent" : "rgba(255,255,255,0.01)",
                    borderBottom: "1px solid var(--border-soft)",
                  }}
                >
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id} style={{ padding: "7px 12px", color: "var(--text-primary)" }}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
