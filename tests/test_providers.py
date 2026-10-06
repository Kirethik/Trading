"""Free-tier provider chain: fallback order, daily caps, resting on 429/401,
missing keys, disabled providers, and the OpenAI-compatible request shape."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from regime_trader.agents import providers
from regime_trader.agents.providers import Chain, ProviderError, Route, Usage
from regime_trader.agents.schemas import OverfitCaution

PROVIDERS = {
    "gemini": {"kind": "adk", "key_env": "G", "daily_requests": {"g-flash": 2}},
    "groq": {"kind": "openai", "base_url": "https://groq.test/v1", "key_env": "Q",
             "daily_requests": {"big": 5}},
    "openrouter": {"kind": "openai", "base_url": "https://or.test/v1", "key_env": "O",
                   "default_daily_requests": 3},
    "mistral": {"enabled": False, "kind": "openai", "base_url": "https://m.test/v1", "key_env": "M"},
}
ENV = {"G": "g", "Q": "q", "O": "o", "M": "m"}
ROUTES = [Route("mistral", "small"), Route("gemini", "g-flash"), Route("groq", "big"), Route("openrouter", "free")]


class Spec:
    name = "t"


def make(tmp_path, http, gemini=None, env=ENV, routes=ROUTES):
    async def gem(spec, system, user, model, tally, name):
        if gemini is None:
            raise RuntimeError("429 RESOURCE_EXHAUSTED free_tier_requests per day")
        return gemini
    return Chain(routes, providers=PROVIDERS, usage=Usage(tmp_path / "u.json"), gemini_call=gem,
                 http_call=http, env=env)


def run(chain):
    return asyncio.run(chain(Spec(), "sys", "user", OverfitCaution))


def http_ok(reply='{"caution":"c","overfit_risk":"low"}'):
    calls = []

    def call(base, key, model, system, user, output_model, **kw):
        calls.append((base, model))
        return reply, 10, 5
    call.calls = calls
    return call


def test_first_available_route_answers(tmp_path):
    ch = make(tmp_path, http_ok(), gemini=('{"caution":"g","overfit_risk":"low"}', 1, 1))
    text, *_ = run(ch)
    assert '"g"' in text and ch.last_route == Route("gemini", "g-flash")
    assert ch.tried[0] == "mistral:small: off"                 # disabled provider skipped


def test_daily_quota_on_gemini_falls_back_and_rests_until_tomorrow(tmp_path):
    http = http_ok()
    ch = make(tmp_path, http)                                    # gemini raises daily 429
    run(ch)
    assert ch.last_route == Route("groq", "big")
    rest = Usage(tmp_path / "u.json").snapshot()["rest"]["gemini:g-flash"]
    assert datetime.fromisoformat(rest) > datetime.now(timezone.utc)
    ch2 = make(tmp_path, http)
    run(ch2)
    assert "gemini:g-flash: resting/at cap" in ch2.tried         # not retried today


def test_per_minute_limit_rests_briefly(tmp_path):
    def limited(base, key, model, *a, **k):
        if "groq" in base:
            raise ProviderError(429, "Rate limit reached: requests per minute", retry_after=7)
        return '{"caution":"c","overfit_risk":"low"}', 1, 1
    ch = make(tmp_path, limited, routes=[Route("groq", "big"), Route("openrouter", "free")])
    run(ch)
    rest = datetime.fromisoformat(Usage(tmp_path / "u.json").snapshot()["rest"]["groq:big"])
    assert (rest - datetime.now(timezone.utc)).total_seconds() < 120


def test_bad_key_and_missing_key(tmp_path):
    def unauthorized(base, *a, **k):
        if "groq" in base:
            raise ProviderError(401, "invalid api key")
        return '{"caution":"c","overfit_risk":"low"}', 1, 1
    ch = make(tmp_path, unauthorized, env={"Q": "q"},          # no gemini/openrouter keys
              routes=[Route("gemini", "g-flash"), Route("groq", "big"), Route("openrouter", "free")])
    with pytest.raises(providers.AllProvidersExhausted) as err:
        run(ch)
    assert "gemini:g-flash: no key" in str(err.value) and "groq:big: 401" in str(err.value)


def test_our_daily_cap_is_never_exceeded(tmp_path):
    http = http_ok()
    for _ in range(3):
        run(make(tmp_path, http, routes=[Route("openrouter", "free")]))  # cap 3
    with pytest.raises(providers.AllProvidersExhausted):
        run(make(tmp_path, http, routes=[Route("openrouter", "free")]))
    assert len(http.calls) == 3


def test_network_error_moves_on_without_resting(tmp_path):
    def flaky(base, *a, **k):
        if "groq" in base:
            raise ProviderError(0, "network: ConnectionError")
        return '{"caution":"c","overfit_risk":"low"}', 1, 1
    ch = make(tmp_path, flaky, routes=[Route("groq", "big"), Route("openrouter", "free")])
    run(ch)
    assert ch.last_route.provider == "openrouter"
    assert "groq:big" not in (Usage(tmp_path / "u.json").snapshot().get("rest") or {})


def test_openai_compatible_payload():
    sent = {}

    class Resp:
        status_code = 200
        headers = {}

        def json(self):
            return {"choices": [{"message": {"content": '{"x":1}'}}], "usage": {"prompt_tokens": 3,
                                                                              "completion_tokens": 2}}

    class S:
        def post(self, url, json=None, headers=None, timeout=None):
            sent.update(url=url, body=json, headers=headers)
            return Resp()
    text, tin, tout = providers.openai_compatible_call("https://api.groq.com/openai/v1/", "KEY", "m", "sys", "u",
                                                       OverfitCaution, timeout=5, temperature=0.2, session=S())
    assert sent["url"] == "https://api.groq.com/openai/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer KEY"
    assert sent["body"]["response_format"] == {"type": "json_object"}
    assert "overfit_risk" in sent["body"]["messages"][1]["content"]    # schema travels in the prompt
    assert (text, tin, tout) == ('{"x":1}', 3, 2)


def test_route_parsing_keeps_model_colons():
    r = providers.parse_route("openrouter:nvidia/nemotron-3-super-120b-a12b:free")
    assert r == Route("openrouter", "nvidia/nemotron-3-super-120b-a12b:free")


def test_runtime_records_which_provider_answered(tmp_path, monkeypatch):
    from regime_trader.agents import audit, budget, guards, runtime, specs
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setattr(guards, "kill_switch_engaged", lambda path=None: False)
    ch = make(tmp_path, http_ok(), routes=[Route("groq", "big")])
    monkeypatch.setattr(providers, "Chain", lambda routes, gemini_call=None: ch)
    monkeypatch.setattr(providers, "routes_for", lambda name: [Route("groq", "big")])
    log = audit.AuditLog(tmp_path / "a.jsonl", 10_000_000)
    out = runtime.run_agent(specs.get_spec("overfitting-detection"), "i", {"strategy": "s"},
                            {"caution": "s", "overfit_risk": "low"},
                            budget=budget.Budget(tmp_path / "b.json", 2.0, {"free": {"input": 0, "output": 0}}),
                            audit=log, alert=lambda m: None)
    assert out == {"caution": "c", "overfit_risk": "low"}
    assert log.tail(1)[0]["model"] == "groq:big"
