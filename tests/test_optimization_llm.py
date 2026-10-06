"""Daily improver: LLM-proposed parameter sets in the optimization team.

llm_bridge.escalate and the backtest Sharpe are monkeypatched — no claude CLI,
no network, no real backtests.
"""
from __future__ import annotations

from regime_trader.govern import llm_bridge, optimization
from regime_trader.govern.registry import GovernedStrategy
from regime_trader.strategy.trend import TimeSeriesMomentum


def _gs(lookback=63, skip=0) -> GovernedStrategy:
    return GovernedStrategy(key="tsmom-test-eth", strategy=TimeSeriesMomentum(lookback, skip),
                            symbols=["ETH/USD"], market="crypto", source="omnivore")


# ------------------------------------------------------- llm_propose_params
def test_llm_propose_params_sanitizes(monkeypatch):
    monkeypatch.setattr(llm_bridge, "escalate", lambda *a, **k: {
        "candidates": [
            {"lookback": 30, "skip": 5},          # valid
            {"params": {"lookback": 40.4}},        # tolerated wrapping; float -> int
            {"bogus_attr": 12},                    # unknown attr only -> dropped
            {"lookback": "not-a-number"},          # uncoercible -> dropped
            {"lookback": 63, "skip": 0},           # identical to current -> dropped
        ],
        "rationale": "x",
    })
    cands = optimization.llm_propose_params(_gs(), base=0.5, max_candidates=5)
    assert cands == [{"lookback": 30, "skip": 5}, {"lookback": 40}]


def test_llm_propose_params_none_on_agent_failure(monkeypatch):
    monkeypatch.setattr(llm_bridge, "escalate", lambda *a, **k: None)
    assert optimization.llm_propose_params(_gs(), base=0.5, max_candidates=3) == []


# ------------------------------------------------------------------ propose
def _fake_sharpe_factory(table, default=0.4):
    def fake(bars, strategy, symbols, oracle):
        return table.get(getattr(strategy, "lookback", None), default)
    return fake


def test_propose_adopts_winning_llm_candidate(monkeypatch):
    # base lookback 63 -> 0.5; LLM candidate 30 -> 1.5; greedy neighbors -> 0.4
    monkeypatch.setattr(optimization, "_sharpe", _fake_sharpe_factory({63: 0.5, 30: 1.5}))
    monkeypatch.setattr(llm_bridge, "escalate", lambda *a, **k: {
        "candidates": [{"lookback": 30}], "rationale": "shorter horizon for crypto"})

    p = optimization.propose(_gs(), bars={"ETH/USD": object()}, oracle=None,
                             deep=False, use_llm=True)
    assert p is not None and p.improves
    assert p.changes == {"lookback": 30}
    assert p.proposed_sharpe == 1.5
    assert "LLM-proposed" in p.rationale
    assert p.proposed_strategy.lookback == 30


def test_propose_ignores_llm_when_not_better(monkeypatch):
    # LLM candidate scores below base -> no proposal at all (greedy also flat)
    monkeypatch.setattr(optimization, "_sharpe", _fake_sharpe_factory({63: 0.5, 30: 0.1}))
    monkeypatch.setattr(llm_bridge, "escalate", lambda *a, **k: {
        "candidates": [{"lookback": 30}], "rationale": "x"})
    assert optimization.propose(_gs(), bars={"ETH/USD": object()}, oracle=None,
                                deep=False, use_llm=True) is None


def test_propose_without_llm_never_calls_agent(monkeypatch):
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        return None

    monkeypatch.setattr(llm_bridge, "escalate", boom)
    monkeypatch.setattr(optimization, "_sharpe", _fake_sharpe_factory({63: 0.5}))
    optimization.propose(_gs(), bars={"ETH/USD": object()}, oracle=None,
                         deep=False, use_llm=False)
    assert called["n"] == 0


# --------------------------------------------------------- run_optimization
def test_run_optimization_llm_keys_plumbing(monkeypatch):
    monkeypatch.setattr(optimization, "_sharpe", _fake_sharpe_factory({63: 0.5, 30: 1.5}))
    monkeypatch.setattr(llm_bridge, "escalate", lambda *a, **k: {
        "candidates": [{"lookback": 30}], "rationale": "x"})
    gs = _gs()

    out = optimization.run_optimization([gs], bars={"ETH/USD": object()}, oracle=None,
                                        deep=False, only_keys={gs.key}, llm_keys={gs.key})
    assert gs.key in out and out[gs.key].changes == {"lookback": 30}

    # not in llm_keys -> greedy only -> no improvement -> no proposal
    out2 = optimization.run_optimization([gs], bars={"ETH/USD": object()}, oracle=None,
                                         deep=False, only_keys={gs.key}, llm_keys=set())
    assert out2 == {}
