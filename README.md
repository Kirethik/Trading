# Regime-Trader

A Python trading system that classifies the market's **volatility regime**
(calm / moderate / turbulent) and adjusts allocation and leverage to match.
Always long, never short. Paper trading by default.

## Philosophy

**Risk management > signal generation.** The measured edge is *avoiding large
drawdowns*, not predicting tops or bottoms. Calm markets tend to trend up —
that is where returns come from, so the system is fully invested (with modest
leverage) when volatility is low, trims when the trend breaks in mid
volatility, and shrinks — but never flattens, never flips short — in
turbulence, so V-shaped rebounds are never missed.

An independent risk layer holds absolute veto power over every signal from
every model. It operates on actual P&L, so it keeps working even if the
regime model fails completely.

## Architecture

```
data (Alpaca, cached)            config/config.yaml (every tunable)
   └─ features (strictly causal)         .env (credentials only)
        └─ regime engine: Gaussian HMM, BIC-selected 3–7 states,
           FILTERED (forward-only) inference — never smoothing
             └─ stability: confirmation, transition damping, flicker→uncertainty
                  └─ allocation: vol-rank-scaled exposure curve (regime-count agnostic)
                       └─ RISK LAYER (veto): stops required, sizing from stop distance,
                          portfolio caps, tiered circuit breakers, hard DD lock
                            └─ broker: Alpaca wrapper (paper default, brackets,
                               tighten-only stops, reconciliation, fill stream)
                                 └─ monitoring: rotating logs, alerts, live dashboard
```

Modules under `regime_trader/`: `data/`, `regime/`, `strategy/`, `risk/`,
`broker/`, `backtest/`, `live/`, `monitor/`, `omnivore/` — fully separable;
the backtest engine and the live orchestrator share the same strategy and
risk code.

## Zero look-ahead bias

The regime at time T is identical whether computed from data ending at T or
years later — guaranteed by construction (hand-rolled forward filtering; the
hmmlearn smoothing APIs are never called) and proven by automated tests
(`tests/test_no_lookahead.py`): filtered-probability truncation invariance,
feature/indicator truncation invariance, and bit-identical backtest equity
across different end dates. The walk-forward oracle only ever consumes
out-of-sample rows from models trained strictly on earlier bars.

## Model choices — and rejections

- **Gaussian HMM on [log return, log realized vol, intraday range]**, state
  count selected by BIC across 3–7. Chosen because regimes are about the
  *distribution* of returns, not their direction.
- **Filtered inference only.** `predict()`/`predict_proba()` (forward-backward
  smoothing) were rejected: they let the regime at T see the future.
- **Vol-rank drives strategy; labels are cosmetic.** Strategies interpolate a
  continuous exposure curve over vol rank, so 3-state and 7-state models need
  no code changes. Human labels ("calm-bull") exist only for readability.
- **Confirmation + flicker guards.** Raw regime flips only count after
  persisting; flickering forces uncertainty mode (half size, no leverage).
  Rejected: trading raw argmax states — it whipsaws.
- **Random-allocation benchmark with the same risk rules** answers whether
  the edge is the model or just the risk layer.

## The competing-strategy bracket (§5) and ensemble (§6)

`python -m regime_trader compare` runs 11 classic systematic strategies
(TSMOM 12-1, cross-sectional momentum, SMA-200 trend, Donchian 55/20,
RSI(2) pullback, Bollinger reversion, vol targeting, dual momentum, TAA
rotation, breadth overlay, and the incumbent) through the **identical**
walk-forward engine, risk layer, slippage and fill delay, on identical bars.
Each entrant must pass the same look-ahead and crash-stress gates before it
ranks. Survivors vote in an ensemble whose risk budget is set by the regime
model; the ensemble is kept only if it beats the best single strategy.

## Omnivore mode (§13)

`omni-deploy` stamps out ~30 parameterized strategy variants across stocks,
ETFs and Alpaca crypto, gives each a fixed capital slice, and forward-tests
the whole cohort concurrently on the paper account ("deploy first, correct
later"). Every order is tagged with its strategy id; a persistent ledger
attributes P&L per strategy; `omni-report` ranks them and `omni-prune`
retires proven losers. The global hard-drawdown lock halts the entire cohort.

## Agent runtime (Gemini, paper only)

`regime_trader/agents/` is the multi-agent layer, built on Google's Agent
Development Kit with Gemini models. It is **paper only, permanently**: the
runtime raises `PaperOnlyViolation` unless `ALPACA_PAPER` is true, and every
order tool checks this again.

- **Governance escalations.** `govern/llm_bridge.escalate()` is still the only
  way governance code reaches an LLM. It now runs a Gemini agent whose system
  prompt is the rubric in `.claude/skills/<skill>/SKILL.md`, and the reply is
  checked against a strict pydantic schema (`agents/schemas.py`). If anything
  fails (no key, kill switch, budget, timeout, invalid JSON), it returns
  `None` and the deterministic rules decide.
- **Autonomous paper orders.** `agents/execution.py` gives agents a
  `place_paper_order` tool, and no human approval is needed. Each order still
  goes through `RiskManager.evaluate` (stop required, caps, breakers, hard
  lock) and the same submission path as the orchestrator (`live/execution.py`).
  Agents trade their own sleeve of the account (`agents.execution.max_sleeve_pct`,
  default 25%) and are capped at `max_orders_per_day`.
- **Tool tiers.** Every tool is READ, PROPOSE or EXECUTE_PAPER. An agent can
  only call tools at or below its own tier that are also on its allowlist.
  Unregistered tools are always blocked.
- **Safety rails.**
  - Daily USD budget (`agents.daily_budget_usd`).
  - Kill switch: `touch state/agents.disabled` pauses every agent.
  - Append-only audit log in `state/agent_audit.jsonl`.
  - `AGENT` webhook alerts for budget exhaustion, failure streaks, orders and
    supervisor start/stop.
- **Supervisor.** `agents-supervisor` is the always-on process
  (`scripts/regime-agents.service`). It writes a heartbeat to
  `state/agent_supervisor.json`. Trading never depends on it.
- **Blackboard.** `state/agents.db` is a SQLite topic log that agents use to
  share findings.
- **Dashboard.** The `/agents` page in the web app is backed by
  `GET /api/agents/activity`.

Use a **billing-enabled** AI Studio key. On the free tier, Google may use
prompts (which contain your strategies and P&L) to improve its products.

## Commands

```
python -m regime_trader train          # fit + save regime model, show BIC table
python -m regime_trader backtest       # walk-forward + per-regime/confidence proofs + benchmarks
python -m regime_trader compare        # §5 bracket leaderboard + §6 ensemble verdict
python -m regime_trader stress-test    # crash / gaps / scrambled-regime suite
python -m regime_trader dry-run        # one full live cycle, no orders sent
python -m regime_trader live           # paper trading loop
python -m regime_trader dashboard      # live terminal dashboard
python -m regime_trader omni-deploy --orders --loop   # §13 cohort on paper
python -m regime_trader omni-report    # cohort ranking
python -m regime_trader broker-check --round-trip     # Alpaca round-trip test
python -m regime_trader release-lock   # manually release the hard DD lock
python -m regime_trader agents-supervisor   # always-on Gemini agent supervisor (paper only)
```

## The live-trading switch

Everything defaults to **paper**. Going live requires *both*:
1. `python -m regime_trader live --live`
2. typing the confirmation phrase from `config.yaml`
   (`broker.live_confirmation_phrase`) at the interactive prompt.

The broker class refuses to construct a live client without that exact
phrase. There is deliberately no flag, env var, or config switch that skips
the typed confirmation.

## Setup

```
pip install -r requirements.txt
# put credentials in .env (never in config):
#   ALPACA_API_KEY=...  ALPACA_SECRET_KEY=...  ALPACA_PAPER=true
#   GOOGLE_API_KEY=...     # Gemini agents (optional: without it, deterministic-only)
pytest                      # incl. the look-ahead proof
python -m regime_trader train
```

## Known assumptions

- Commission-free (explicit in `backtest.commission`); slippage 5 bps/side.
- Daily bars; fills modeled at next bar's open; no margin interest modeled.
- Stocks get server-side bracket stops; crypto stops are software-enforced
  each cycle (Alpaca crypto has no native stop orders).
- Scraped/web data (VIX, breadth, macro calendar — §12) is wired as
  *advisory only*: it may shrink risk, never grow it, never override the veto.
