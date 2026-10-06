"use client";
import { create } from "zustand";
import type { BacktestPayload, BacktestRequest } from "@/lib/api";

export interface BacktestParams {
  strategy: string;
  symbol: string;
  timeframe: string;
  amount: number;
  start_date: string;
  end_date: string;
  params: Record<string, number>;
}

interface BacktestState {
  params: BacktestParams;
  result: BacktestPayload | null;
  isLoading: boolean;
  error: string | null;
  compareSet: string[];   // param_hashes pinned for comparison

  setParams: (p: Partial<BacktestParams>) => void;
  setResult: (r: BacktestPayload | null) => void;
  setLoading: (v: boolean) => void;
  setError: (e: string | null) => void;
  addToCompare: (hash: string) => void;
  removeFromCompare: (hash: string) => void;
  clearCompare: () => void;

  toRequest: () => BacktestRequest;
}

const DEFAULT_PARAMS: BacktestParams = {
  strategy:  "ai-enhanced",
  symbol:    "ETH/USD",
  timeframe: "1Day",
  amount:    10_000,
  start_date: "2021-01-01",
  end_date:   new Date().toISOString().slice(0, 10),
  params: {},
};

export const useBacktestStore = create<BacktestState>((set, get) => ({
  params:     DEFAULT_PARAMS,
  result:     null,
  isLoading:  false,
  error:      null,
  compareSet: [],

  setParams:  (p) => set((s) => ({ params: { ...s.params, ...p } })),
  setResult:  (r) => set({ result: r }),
  setLoading: (v) => set({ isLoading: v }),
  setError:   (e) => set({ error: e }),

  addToCompare: (hash) =>
    set((s) => ({
      compareSet: s.compareSet.includes(hash) ? s.compareSet : [...s.compareSet, hash].slice(-4),
    })),
  removeFromCompare: (hash) =>
    set((s) => ({ compareSet: s.compareSet.filter((h) => h !== hash) })),
  clearCompare: () => set({ compareSet: [] }),

  toRequest: (): BacktestRequest => {
    const p = get().params;
    return {
      strategy:   p.strategy,
      symbol:     p.symbol,
      timeframe:  p.timeframe,
      amount:     p.amount,
      start_date: p.start_date || undefined,
      end_date:   p.end_date   || undefined,
      params:     Object.keys(p.params).length ? p.params : undefined,
    };
  },
}));
