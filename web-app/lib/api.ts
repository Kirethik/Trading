/**
 * Typed API client for the FastAPI backend.
 * All requests go through the same session cookie the legacy dashboard uses.
 */

// Empty string = same-origin (production behind nginx). Dev overrides via .env.local.
const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...init?.headers },
    ...init,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail ?? `HTTP ${res.status}`);
  }
  return res.json() as Promise<T>;
}

// ------------------------------------------------------------------ types
export interface Asset {
  symbol: string;
  label: string;
  class: "crypto" | "stock" | "etf";
}

export interface ParamDef {
  name: string;
  label: string;
  type: "int" | "float";
  default: number;
  min: number;
  max: number;
  step: number;
}

export interface StrategyDef {
  key: string;
  label: string;
  family: string;
  description: string;
  params: ParamDef[];
  needs_regime: boolean;
}

export interface Metrics {
  total_return: number;
  cagr: number;
  sharpe: number;
  sortino: number;
  calmar: number;
  max_drawdown: number;
  max_drawdown_duration_bars: number;
  recovery_factor: number;
  ann_vol: number;
  win_rate: number;
  profit_factor: number;
  n_round_trips?: number;
  avg_trade?: number;
  bars: number;
}

export interface OhlcBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export interface Signal {
  ts: number;
  price: number;
  side: "buy" | "sell";
  reason: string;
}

export interface MonthlyReturn {
  year: number;
  month: number;
  return: number;
}

export interface YearlyReturn {
  year: number;
  return: number;
}

export interface RollingPoint {
  ts: number;
  value: number;
}

export interface RegimeRow {
  regime: string;
  bars: number;
  ann_return: number;
  sharpe: number;
  mean_vol_rank?: number;
}

export interface TradeRegion {
  symbol: string;
  entry_ts: number;       // ms epoch
  entry_price: number;
  exit_ts: number;        // ms epoch (= last bar if is_open)
  exit_price: number | null;
  holding_days: number;
  return_pct: number | null;
  is_open: boolean;
  profitable: boolean | null;
}

export interface BacktestPayload {
  run_id: string;
  param_hash: string;
  from_cache: boolean;
  strategy: string;
  symbol: string;
  timeframe: string;
  amount: number;
  metrics: Metrics;
  equity: [number, number][];
  drawdown: [number, number][];
  price: OhlcBar[];
  signals: Signal[];
  trades: TradeRegion[];
  monthly: MonthlyReturn[];
  yearly: YearlyReturn[];
  rolling_sharpe: RollingPoint[];
  rolling_vol: RollingPoint[];
  per_regime: RegimeRow[];
  benchmark: {
    equity: [number, number][];
    metrics: Partial<Metrics>;
  };
}

export interface BacktestRequest {
  strategy: string;
  symbol: string;
  timeframe: string;
  amount: number;
  start_date?: string | null;
  end_date?: string | null;
  params?: Record<string, number>;
  force_refresh?: boolean;
}

export interface CompareRow {
  param_hash: string;
  strategy: string;
  symbol: string;
  sharpe: number | null;
  cagr: number | null;
  max_drawdown: number | null;
  calmar: number | null;
  profit_factor: number | null;
  win_rate: number | null;
  total_return: number | null;
}

// ---------------------------------------------------------- agent activity
export interface AgentEvent {
  ts: string;
  agent: string;
  kind?: string;
  outcome?: string;          // model calls: ok | invalid | timeout | error | blocked
  status?: string;           // orders: submitted | vetoed | noop | deferred | ...
  model?: string;
  cost_usd?: number;
  latency_ms?: number;
  symbol?: string;
  side?: string;
  target_weight?: number;
  reason?: string | null;
  error?: string | null;
  rationale?: string;
}

export interface AgentActivityPayload {
  as_of: string;
  events: AgentEvent[];
  supervisor: { ts: string; pid: number; status: string; age_seconds: number;
                jobs: Record<string, { runs: number; last_run: string | null; last_error: string | null }> } | null;
  budget: { date: string; spent_usd: number; calls: number; daily_usd: number; by_model: Record<string, number> };
  kill_switch: boolean;
  sleeve: { positions: Record<string, { qty: number; entry: number; stop: number; price: number }>;
            day: { date: string; orders: number } | null };
}

// ---------------------------------------------------- live portfolio / ops
export interface PortfolioTotals {
  total_invested: number;
  total_cash: number;
  total_market_value: number;
  total_equity: number;
  total_pnl: number;
  total_pnl_pct: number;
  total_realized_pnl: number;
  total_unrealized_pnl: number;
  strategies: number;
  enabled: number;
  open_positions: number;
  symbols: number;
  symbols_priced: number;
}

export interface PortfolioPosition {
  strategy: string;
  symbol: string;
  qty: number;
  entry: number;
  price: number;
  value: number;
  unrealized_pnl: number;
  unrealized_pnl_pct: number;
  stale: boolean;
}

export interface PortfolioStrategy {
  strategy: string;
  market: string;
  enabled: boolean;
  allocated: number;
  cash: number;
  market_value: number;
  equity: number;
  total_pnl: number;
  pnl_pct: number;
  realized_pnl: number;
  open_positions: number;
}

export interface PortfolioPayload {
  as_of: string;
  live: boolean;
  totals: PortfolioTotals;
  strategies: PortfolioStrategy[];
  positions: PortfolioPosition[];
}

export interface TradeRow {
  ts: string;
  strategy: string;
  symbol: string;
  side: string;
  kind: string;
  requested: number | null;
  final: number | null;
  approved: boolean;
  reason: string;
}

export interface TradesPayload {
  as_of: string;
  rows: TradeRow[];
  summary: { approved: number; blocked: number; fills: number; symbols: string[] };
}

export interface WatchSymbol {
  symbol: string;
  label: string;
  cls: string;
  held: boolean;
  qty: number;
  held_by: string;
}

export interface WatchStrategy {
  key: string;
  status: string;
  market: string;
  source: string;
  allocation: number;
  last_validated: string;
}

export interface WatchlistPayload {
  as_of: string;
  symbols: WatchSymbol[];
  strategies: WatchStrategy[];
  n_symbols: number;
  n_held: number;
  n_strategies: number;
}

export interface LearningPayload {
  as_of: string;
  regime: { states: number; bic: Record<string, number>; ts: string } | null;
  regime_fit_histogram: Record<string, number>;
  risk_reasons: { reason: string; count: number }[];
  strategy_insights: { key: string; decision: string; rationale: string; ts: string }[];
}

// ------------------------------------------------------------------ endpoints
export const api = {
  assets: (): Promise<{ assets: Asset[] }> =>
    req("/api/v1/assets"),

  strategies: (): Promise<{ strategies: StrategyDef[] }> =>
    req("/api/v1/strategies"),

  price: (symbol: string, timeframe: string, start?: string, end?: string) =>
    req<{ symbol: string; bars: OhlcBar[] }>(
      `/api/v1/price?symbol=${encodeURIComponent(symbol)}&timeframe=${timeframe}${start ? `&start=${start}` : ""}${end ? `&end=${end}` : ""}`
    ),

  backtest: (body: BacktestRequest): Promise<BacktestPayload> =>
    req("/api/v1/backtest", { method: "POST", body: JSON.stringify(body) }),

  backtestGet: (hash: string): Promise<BacktestPayload> =>
    req(`/api/v1/backtest/${hash}`),

  compare: (hashes: string[]): Promise<{ rows: CompareRow[] }> =>
    req("/api/v1/compare", { method: "POST", body: JSON.stringify({ runs: hashes }) }),

  cacheStatus: (): Promise<{ redis_available: boolean }> =>
    req("/api/v1/cache/status"),

  session: (): Promise<{ authed: boolean; password_set: boolean; title: string }> =>
    req("/api/session"),

  login: (password: string): Promise<{ ok: boolean }> =>
    req("/login", { method: "POST", body: JSON.stringify({ password }) }),

  // live operational data (session-gated, same cookie as the legacy dashboard)
  portfolio: (): Promise<PortfolioPayload> =>
    req("/api/portfolio"),

  trades: (limit = 200): Promise<TradesPayload> =>
    req(`/api/trades?limit=${limit}`),

  watchlist: (): Promise<WatchlistPayload> =>
    req("/api/watchlist"),

  learning: (): Promise<LearningPayload> =>
    req("/api/learning"),

  agentActivity: (limit = 200): Promise<AgentActivityPayload> =>
    req(`/api/agents/activity?limit=${limit}`),
};
