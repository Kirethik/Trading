"""DataProvider.get_latest_prices(): best-effort live quotes for mark-to-market.

Alpaca clients are replaced with fakes -- no network/credentials needed.
"""
from __future__ import annotations

from regime_trader.data.provider import DataProvider


class _FakeTrade:
    def __init__(self, price):
        self.price = price


class _FakeStockClient:
    def __init__(self, prices, fail_feeds=()):
        self.prices = prices
        self.fail_feeds = set(fail_feeds)
        self.calls = []

    def get_stock_latest_trade(self, req):
        feed = req.feed.value if req.feed else None
        self.calls.append(feed)
        if feed in self.fail_feeds:
            raise RuntimeError(f"feed {feed} not permitted")
        return {sym: _FakeTrade(p) for sym, p in self.prices.items() if sym in req.symbol_or_symbols}


class _FakeCryptoClient:
    def __init__(self, prices):
        self.prices = prices

    def get_crypto_latest_trade(self, req):
        return {sym: _FakeTrade(p) for sym, p in self.prices.items() if sym in req.symbol_or_symbols}


def test_get_latest_prices_empty_input_returns_empty():
    assert DataProvider().get_latest_prices([]) == {}


def test_get_latest_prices_stocks_and_crypto():
    provider = DataProvider()
    provider._stock_client = _FakeStockClient({"SPY": 410.5, "AAPL": 190.2})
    provider._crypto_client = _FakeCryptoClient({"BTC/USD": 65000.0})

    prices = provider.get_latest_prices(["SPY", "AAPL", "BTC/USD"])

    assert prices == {"SPY": 410.5, "AAPL": 190.2, "BTC/USD": 65000.0}


def test_get_latest_prices_falls_back_to_iex_on_sip_failure():
    provider = DataProvider()
    fake = _FakeStockClient({"SPY": 410.5}, fail_feeds={"sip"})
    provider._stock_client = fake

    prices = provider.get_latest_prices(["SPY"])

    assert prices == {"SPY": 410.5}
    assert fake.calls == ["sip", "iex"]


def test_get_latest_prices_returns_partial_on_total_failure():
    provider = DataProvider()
    provider._stock_client = _FakeStockClient({"SPY": 410.5}, fail_feeds={"sip", "iex"})

    assert provider.get_latest_prices(["SPY"]) == {}
