"""News store (point-in-time) + deterministic news rules. No network, no model."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from regime_trader.news import rules
from regime_trader.news.store import Article, NewsStore

T0 = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)


def art(sid, sym="NVDA", source="alpaca", pub=T0, seen=T0, headline="h"):
    return Article(source=source, source_id=sid, symbols=(sym,), published_at=pub, seen_at=seen,
                   headline=headline, summary="s", url=f"https://x.test/{sid}", body="")


# -------------------------------------------------------------------- store
def test_store_dedups_and_filters_by_symbol(tmp_path):
    st = NewsStore(tmp_path / "n.db")
    assert st.add_articles([art("1"), art("1"), art("2", sym="AAPL")]) == 2
    assert [a["id"] for a in st.articles(symbol="NVDA")] == ["alpaca:1"]


def test_store_is_point_in_time(tmp_path):
    st = NewsStore(tmp_path / "n.db")
    st.add_articles([art("old", pub=T0 - timedelta(hours=2), seen=T0 - timedelta(hours=1)),
                     art("future", pub=T0 + timedelta(hours=1), seen=T0 + timedelta(hours=1)),
                     art("late-seen", pub=T0 - timedelta(hours=3), seen=T0 + timedelta(minutes=5)),
                     art("exa", source="exa", pub=T0 - timedelta(days=9), seen=T0 + timedelta(minutes=1))])
    ids = {a["id"] for a in st.articles(symbol="NVDA", as_of=T0)}
    assert ids == {"alpaca:old"}            # published AND seen before T0


def test_assessments_and_actions_round_trip(tmp_path):
    st = NewsStore(tmp_path / "n.db")
    aid = st.add_assessment({"symbol": "NVDA", "as_of": T0, "sentiment": 1.8, "confidence": "high",
                             "impact": "opportunity", "why": "w", "article_ids": ["alpaca:1"], "model": "m"})
    assert st.latest_assessment("NVDA", as_of=T0)["id"] == aid
    assert st.latest_assessment("NVDA", as_of=T0 - timedelta(seconds=1)) is None
    st.add_action({"ts": T0, "symbol": "NVDA", "kind": "caution", "reason": "r", "assessment_id": aid,
                   "result": "ok", "details": {}})
    assert set(st.open_cautions(as_of=T0 + timedelta(hours=1), hours=24)) == {"NVDA"}
    assert st.open_cautions(as_of=T0 + timedelta(hours=25), hours=24) == {}


def test_news_holdings_track_buys_until_exit(tmp_path):
    st = NewsStore(tmp_path / "n.db")
    st.add_action({"ts": T0, "symbol": "NVDA", "kind": "buy", "reason": "r", "assessment_id": None,
                   "result": "submitted", "details": {"weight": 0.05}})
    st.add_action({"ts": T0, "symbol": "AAPL", "kind": "buy", "reason": "r", "assessment_id": None,
                   "result": "vetoed", "details": {"weight": 0.05}})
    assert set(st.news_holdings()) == {"NVDA"}
    st.add_action({"ts": T0 + timedelta(days=1), "symbol": "NVDA", "kind": "exit", "reason": "r",
                   "assessment_id": None, "result": "submitted", "details": {}})
    assert st.news_holdings() == {}


# -------------------------------------------------------------------- rules
SETTINGS = {"buy_threshold": 1.5, "caution_threshold": -1.5, "buy_weight": 0.05, "max_news_buys_per_day": 3,
            "max_news_exposure": 0.40, "min_sources": 2, "hold_days": 5, "exit_sentiment": -1.0}


def assess(sym="NVDA", s=1.8, conf="high", impact="opportunity"):
    return rules.Assessment(id=1, symbol=sym, sentiment=s, confidence=conf, impact=impact, why="w",
                            article_ids=("alpaca:1", "exa:2"))


def ctx(**kw):
    base = dict(now=T0, allowed=frozenset({"NVDA", "AAPL"}), above_sma50={"NVDA": True, "AAPL": True},
                evidence={"NVDA": rules.Evidence(articles=3, sources=2, point_in_time=True),
                          "AAPL": rules.Evidence(articles=3, sources=2, point_in_time=True)},
                open_cautions=frozenset(), paused=False, holdings={}, buys_today=0)
    return rules.Context(**{**base, **kw})


def kinds(actions):
    return [(a.kind, a.symbol) for a in actions]


def test_strong_corroborated_good_news_buys_small():
    acts = rules.decide({"NVDA": assess()}, ctx(), SETTINGS)
    assert kinds(acts) == [("buy", "NVDA")] and acts[0].weight == 0.05
    assert "corroborated" in acts[0].reason


@pytest.mark.parametrize("change,failed", [
    ({"evidence": {"NVDA": rules.Evidence(3, 1, True)}}, "only 1 independent source"),
    ({"evidence": {"NVDA": rules.Evidence(3, 2, False)}}, "no time-stamped"),
    ({"above_sma50": {"NVDA": False}}, "below its 50-day average"),
    ({"open_cautions": frozenset({"NVDA"})}, "caution"),
    ({"paused": True}, "paused"),
    ({"buys_today": 3}, "daily limit"),
    ({"holdings": {"NVDA": {"opened": T0, "weight": 0.05}}}, "already hold"),
    ({"holdings": {f"S{i}": {"opened": T0, "weight": 0.1} for i in range(4)}}, "40%"),
    ({"allowed": frozenset({"AAPL"})}, "not in the trading universe"),
])
def test_every_buy_gate_blocks_and_explains(change, failed):
    acts = rules.decide({"NVDA": assess()}, ctx(**change), SETTINGS)
    assert [a.kind for a in acts if a.symbol == "NVDA"] in (["considered"], ["considered", "exit"], [])
    considered = [a for a in acts if a.kind == "considered"]
    if considered:
        assert failed in considered[0].reason


def test_weak_or_unsure_positive_news_is_not_considered():
    assert rules.decide({"NVDA": assess(s=1.0)}, ctx(), SETTINGS) == []
    acts = rules.decide({"NVDA": assess(conf="medium")}, ctx(), SETTINGS)
    assert kinds(acts) == [("considered", "NVDA")]


def test_bad_news_triggers_caution_once():
    acts = rules.decide({"NVDA": assess(s=-1.8, impact="risk", conf="medium")}, ctx(), SETTINGS)
    assert kinds(acts) == [("caution", "NVDA")]
    again = rules.decide({"NVDA": assess(s=-1.8, impact="risk")}, ctx(open_cautions=frozenset({"NVDA"})), SETTINGS)
    assert again == []
    assert rules.decide({"NVDA": assess(s=-1.8, conf="low")}, ctx(), SETTINGS) == []


def test_news_holdings_exit_on_time_or_reversal():
    held = {"NVDA": {"opened": T0 - timedelta(days=8), "weight": 0.05}}
    assert ("exit", "NVDA") in kinds(rules.decide({}, ctx(holdings=held), SETTINGS))
    fresh = {"NVDA": {"opened": T0 - timedelta(days=1), "weight": 0.05}}
    assert rules.decide({}, ctx(holdings=fresh), SETTINGS) == []
    acts = rules.decide({"NVDA": assess(s=-1.2, impact="risk")}, ctx(holdings=fresh), SETTINGS)
    assert ("exit", "NVDA") in kinds(acts)


def test_decide_is_pure_and_deterministic():
    a = rules.decide({"NVDA": assess(), "AAPL": assess("AAPL", s=-1.9, impact="risk")}, ctx(), SETTINGS)
    b = rules.decide({"AAPL": assess("AAPL", s=-1.9, impact="risk"), "NVDA": assess()}, ctx(), SETTINGS)
    assert kinds(a) == kinds(b) == [("caution", "AAPL"), ("buy", "NVDA")]
