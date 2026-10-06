"""Populate the local cache with deep-history ETH/USD data for research.

Writes the same CSV files the DataProvider reads, so the backtest/research path
needs no Alpaca call and works fully offline afterwards:

    data_cache/ETH-USD_1Day.csv    (Binance ETHUSDT daily, 2017-08+)
    data_cache/ETH-USD_4Hour.csv   (Binance ETHUSDT native 4h, 2017-08+)
    data_cache/ETH-USD_funding.csv (perp funding, full history — research only)
    data_cache/ETH-USD_oi.csv      (open interest, ~30d window — illustrative)

Usage:
    python scripts/fetch_eth_history.py                 # daily + 4h + funding + OI (Binance)
    python scripts/fetch_eth_history.py --coinbase      # also extend daily to 2016 via Coinbase
    python scripts/fetch_eth_history.py --source binanceus   # geo fallback
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regime_trader.data import external as X  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch deep-history ETH/USD data into the cache.")
    ap.add_argument("--source", default="binance", choices=["binance", "binanceus", "coinbase"],
                    help="primary OHLCV source (default: binance)")
    ap.add_argument("--coinbase", action="store_true",
                    help="also fetch a Coinbase daily series to extend history to 2016 (cross-check)")
    ap.add_argument("--no-funding", action="store_true", help="skip perp funding/OI fetch")
    args = ap.parse_args()

    print(f"[fetch] OHLCV source = {args.source}")
    for tf in ("1Day", "4Hour"):
        try:
            rep = X.update_cache("ETH/USD", tf, source=args.source)
            print(f"[ok] {tf:6s} -> {rep['rows']} bars  {rep['start']}..{rep['end']}  "
                  f"max_gap={rep['max_gap_bars']} bars")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] {tf}: {exc}")

    if args.coinbase:
        try:
            # Coinbase has no native 4h; only extend the daily series.
            safe = "ETH-USD_1Day_coinbase.csv"
            df = X.fetch_coinbase_candles("1Day", "ETH-USD", start="2016-05-01")
            from regime_trader.data.provider import _cache_path
            p = _cache_path("ETH/USD", "1Day").with_name(safe)
            df[["open", "high", "low", "close", "volume"]].to_csv(p)
            print(f"[ok] coinbase daily -> {len(df)} bars  {df.index[0].date()}..{df.index[-1].date()} "
                  f"(saved {p.name} for cross-check)")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] coinbase: {exc}")

    if not args.no_funding:
        try:
            rep = X.update_funding_cache("ETH/USD")
            print(f"[ok] funding -> {rep['rows']} points  {rep['start']}..{rep['end']}")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] funding: {exc}")
        try:
            rep = X.update_oi_cache("ETH/USD")
            print(f"[ok] open-interest -> {rep['rows']} points  {rep['start']}..{rep['end']}  ({rep['note']})")
        except Exception as exc:  # noqa: BLE001
            print(f"[FAIL] open-interest: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
