# Plan — Sub-project #0: Gemini Agent Runtime Foundation (+ migrate existing agents)

## Context
The user wants a fully autonomous multi-agent system on top of Regime-Trader covering four capabilities:
governance v2 (A), live oversight (B), market intelligence (C), autonomous research desk (D).
Agreed decomposition: **#0 foundation → A → B → C → D**, each its own spec/plan cycle. This plan is **#0 only**.

Decisions already made with the user:
- Provider: **Google Gemini via AI Studio key**, framework **Google ADK** (`google-adk` 2.x). All existing Claude agents migrate to Gemini (single provider).
- Autonomy: agents act fully autonomously **on paper only — permanently**, **including placing paper orders themselves with no human approval**. Paper-only becomes a hard invariant.
- Process model: **hybrid** — existing cron jobs stay for batch work; one new long-running **agent supervisor under systemd** for always-on agents (B/C later).
- Visibility: **webhook alerts (existing AlertManager) + "Agent Activity" dashboard page**.
- Agents **may place paper orders autonomously**, but every agent order goes through the same `RiskManager.evaluate` veto as the orchestrator (stops required, caps, circuit breakers, DD lock). Agents can never modify risk limits/breakers or release `breaker.lock`. Deterministic-first; any agent failure → `None` → existing deterministic fallback.

Today the only LLM path is `regime_trader/govern/llm_bridge.py` (shells out to `claude -p`), with 4 call sites
(`optimization.py:55,151`, `governance.py:99,138`), all already handling `None`. That single-choke-point design is preserved.

## Key facts from research (verified 2026-10-05)
- `google-adk` v2.11.0, Python ≥3.10 (EC2 has 3.11 ✓). Auth: `GOOGLE_API_KEY` in `.env`. Docs at adk.dev.
- Use `LlmAgent(name, model, instruction, output_schema=PydanticModel, tools, before_tool_callback, before_model_callback)` + `Runner(app_name, agent, session_service=InMemorySessionService(), auto_create_session=True)`; read `event.is_final_response()` filtered by `event.author`; tokens via `event.usage_metadata`.
- Avoid `LoopAgent` (deprecated in 2.x). Native `output_schema` reliable on Gemini 3 models.
- Models (pin IDs, never `*-latest`): workers `gemini-3.8-flash` ($0.75/$3.75 per 1M), governance `gemini-3.1-pro-preview` ($2/$12).
- **Free tier: Google may use prompts for training + human review → user should use a billing-enabled key** (strategies/P&L are in prompts). Free-tier limits only visible in AI Studio.
- `broker.paper` in config.yaml is **never read**; paper/live is decided by env `ALPACA_PAPER` (`config.py` `alpaca_paper`) + `AlpacaBroker` confirmation phrase (`broker/alpaca.py:38-46`).

## Design — new package `regime_trader/agents/`
Small focused modules (each <250 lines):

| Module | Responsibility |
|---|---|
| `schemas.py` | Pydantic output models: `GovernanceDecision`, `AllocationSelection`, `ParamProposal`, `OverfitCaution` (match today's JSON schemas exactly). |
| `specs.py` | `AgentSpec` (frozen dataclass): name, model tier (`pro`/`flash`), rubric path, output schema, tool tier. Registry keyed by skill name (`governance-protocol`, `overfitting-detection`, `validation-pipeline`). Rubrics loaded from existing `.claude/skills/<skill>/SKILL.md` (no move; they're plain markdown). |
| `runtime.py` | `run_agent(spec, instruction, payload) -> dict | None`: builds `LlmAgent`, runs via `Runner` (sync wrapper `asyncio.run` + `asyncio.wait_for` timeout), validates against pydantic schema, records usage. Any exception → `None` + log. |
| `guards.py` | `assert_paper_only()` — raises if `cfg.alpaca_paper` is false (checked at supervisor start, on every `run_agent`, and inside every order tool call). Tool tiers: `READ`, `PROPOSE`, `EXECUTE_PAPER`. Each `AgentSpec` declares its tier; `before_tool_callback` blocks any tool above the agent's tier or not in its allowlist; `before_model_callback` blocks when kill switch or budget tripped. |
| `execution.py` | **Agent paper-order tool** `place_paper_order(symbol, side, target_weight, rationale)` (EXECUTE_PAPER tier). Builds an `OrderIntent` (strategy=`agent:<name>`, stop via `RiskManager.protective_stop` + ATR exactly like the orchestrator), runs `RiskManager.evaluate`, and submits survivors via `AlpacaBroker(paper=True)` `submit_entry`/`submit_exit`. Returns the `RiskDecision` (approved / modified weight / reasons) to the agent. Limits on top of the risk layer: agent **sleeve cap** (`agents.execution.max_sleeve_pct`, default 25% of paper equity, tracked in `state/agent_ledger.json`), `max_orders_per_day` (default 20), idempotent `signal_id` per agent call, equities only when `broker.is_market_open()`. Every order → audit + `AGENT` alert. |
| `budget.py` | Daily USD cap from token usage × config price table; persisted `state/agent_budget.json` via `utils.atomic_write_json`. Exceeded → calls return `None` + one `AGENT` alert. |
| `audit.py` | Append-only JSONL `state/agent_audit.jsonl`: ts, agent, model, payload hash, output, tokens, cost, latency_ms, outcome (`ok`/`timeout`/`invalid`/`blocked`/`error`). Size-rotated. |
| `blackboard.py` | Minimal shared store for agent collaboration (stdlib `sqlite3`, `state/agents.db`): `post(topic, author, payload)`, `read(topic, since)`. Used by A–D later; foundation only ships + tests it. |
| `supervisor.py` | Long-running process: paper guard, heartbeat to `state/agent_supervisor.json` every N s, job registry (empty hook list + heartbeat job now; B/C register jobs later), SIGTERM graceful shutdown, kill-switch file `state/agents.disabled`. CLI: `python -m regime_trader agents-supervisor`. |

### Migration of existing bridge
- `govern/llm_bridge.py`: keep `available()` / `escalate(role, skill, instruction, payload, schema)` signatures so the 4 callers and `tests/test_optimization_llm.py` stay unchanged. Internals: `available()` = agents enabled + `GOOGLE_API_KEY` present + kill switch off + budget ok; `escalate()` → `specs.get(skill)` → `runtime.run_agent(...)`. Remove subprocess/`claude` CLI code and `_parse` envelope logic. Keep the hard-constraint text in the system instruction.
- Role → model: `governance-protocol` → pro; `overfitting-detection` → flash. (`validation-sonnet` is not called anywhere today — wiring it is sub-project A.)
- Note: `overfitting-detection/SKILL.md` lacks the `candidates` schema that `llm_propose_params` requests — fine, the pydantic `ParamProposal` schema is passed per call.

### Config / env / deps
- `config/config.yaml`: replace `governance.agents` keys (`claude_cli`, `*_agent`) with top-level `agents:` — `enabled`, `models: {pro, flash}`, `timeout_seconds`, `daily_budget_usd` (default 2.0), `pricing` table, `audit_file`, `budget_file`, `blackboard_db`, `kill_switch_file`, `supervisor: {heartbeat_seconds}`. Keep `governance.agents.escalation_enabled` as alias read by bridge (or migrate reads).
- `regime_trader/config.py`: add `google_api_key` property (optional; validated by `available()`, required by supervisor start).
- `requirements.txt`: `google-adk>=2.11,<3`.
- `EC2_DEPLOYMENT.md` + `.env` template: replace `ANTHROPIC_API_KEY` with `GOOGLE_API_KEY`; note paid tier.
- `monitor/alerts.py`: add `AlertKind.AGENT` (budget exhausted, consecutive failure streak ≥3, supervisor start/stop).

### Ops (EC2)
- New `scripts/regime-agents.service` (systemd, `Restart=on-failure`, runs supervisor in venv, `EnvironmentFile=.env`); `scripts/ec2_setup.sh` installs/enables it. Cron entry for governance unchanged.

### Dashboard
- FastAPI (`regime_trader/web/app.py`, follow existing `/api/*` route + auth pattern): `GET /api/agents/activity` → audit tail (last 200), today's budget, supervisor heartbeat age, kill-switch state.
- Next.js `web-app/app/agents/page.tsx` following existing page/component conventions (read `web-app/AGENTS.md` first): activity table, cost-today tile, supervisor status badge.

### Shared execution path (avoid duplicating orchestrator logic)
The orchestrator's per-decision execution (`live/orchestrator.py` ~L195-295: stop/ATR calc, limit offset, `submit_entry`/`submit_exit`, `mark_submitted`, local apply) is extracted into a reusable function (e.g. `live/execution.py: execute_decision(...)`) used by **both** the orchestrator and `agents/execution.py`. Behaviour-preserving refactor, guarded by existing orchestrator tests. Agent positions live in their own ledger sleeve so the regime orchestrator's `session.json` tracking and broker reconciliation (`AlpacaBroker.reconcile`) don't fight over them.

## Reuse (don't reinvent)
- `risk.manager.OrderIntent`, `RiskManager.evaluate`, `RiskManager.protective_stop`, `breaker_status` — `regime_trader/risk/manager.py`
- `AlpacaBroker.submit_entry` / `submit_exit` / `is_market_open` / `positions` — `regime_trader/broker/alpaca.py`
- Omnivore ledger pattern (`state/omnivore_ledger.json`, `omnivore/deploy.py:68`) as the model for the agent sleeve ledger
- `config.get_config()` / `resolve_path()` — `regime_trader/config.py`
- `utils.atomic_write_json`, `utils.utc_now` — `regime_trader/utils.py`
- `monitor.log_setup.get_logger("governance")` + new `"agents"` channel in `CHANNELS`
- `monitor.alerts.AlertManager.send(kind, msg, dedupe_key=...)`
- Existing `None`-fallback behaviour in `governance.py` / `optimization.py` — untouched.

## Build order (TDD per step)
1. `schemas.py`, `guards.py` (paper invariant), `budget.py`, `audit.py` + tests.
2. `specs.py`, `runtime.py` with ADK runner injectable (fake runner in tests).
3. Rewire `llm_bridge.py`; config + requirements + `AlertKind.AGENT`.
4. Extract `live/execution.py` from orchestrator (tests stay green), then `agents/execution.py` paper-order tool + sleeve ledger.
5. `blackboard.py`, `supervisor.py`, CLI command, systemd unit.
6. API route + Next.js page (includes agent orders + sleeve P&L).
7. Docs (EC2_DEPLOYMENT.md, README agent section); save spec to `docs/superpowers/specs/2026-10-05-agent-runtime-foundation-design.md`.

## Out of scope (later sub-projects)
Debate/memory for governance (A), oversight watchers (B), news/macro ingestion (C), code-writing research agents + sandbox (D).

## Verification
- `pytest` — all existing tests green (esp. `test_optimization_llm.py`, `test_governance_apply.py`, `test_no_lookahead.py`).
- Order-tool tests (fake broker + real `RiskManager`): buy without computable stop refused; risk veto (breaker HALT/LOCK) blocks buys but allows sells; sleeve cap and daily order cap enforced; duplicate `signal_id` not resubmitted; `ALPACA_PAPER=false` → tool raises before touching broker; READ/PROPOSE-tier agent calling `place_paper_order` is blocked by callback.
- Opt-in paper smoke (`pytest -m live_paper`): agent places one small paper order end-to-end, visible in Alpaca paper account + audit + dashboard.
- New tests (no network; ADK runner faked): paper guard raises when `ALPACA_PAPER=false`; tool not in allowlist is blocked; budget exhaustion → `escalate` returns `None` + alert; timeout/invalid-schema → `None` + audit `outcome`; kill switch → `available()` false; blackboard post/read; supervisor writes heartbeat and exits cleanly on SIGTERM. Coverage ≥80% on `regime_trader/agents/`.
- Opt-in live smoke (`pytest -m live_llm`, needs real `GOOGLE_API_KEY`): one governance escalation returns a schema-valid decision; audit + budget files updated.
- End-to-end: `python -m regime_trader governance --no-trade` (existing daily cycle) runs with Gemini escalations; `runs/governance/<date>/summary.md` shows escalated decisions; unset `GOOGLE_API_KEY` → same run completes deterministically.
- Supervisor: run locally, confirm heartbeat file updates, `GET /api/agents/activity` returns data, dashboard page renders; touch `state/agents.disabled` → agent calls blocked.
- Review: code-reviewer + security-reviewer pass (key handling, tool guards) before commit.
