"""ETH focus cohort: single-asset ETH/USD variants in the omnivore factory."""
from __future__ import annotations

from regime_trader.config import get_config
from regime_trader.omnivore.factory import generate_cohort

EXPECTED_ETH_IDS = {
    "tsmom21-0-eth", "tsmom63-7-eth", "tsmom126-14-eth",
    "sma20-eth", "sma50-eth", "sma100-eth",
    "don10-5-eth", "don20-10-eth", "don55-20-eth",
    "rsi2-10-70-eth", "boll20-2.0-eth",
    "voltarget-20-eth", "voltarget-30-eth",
}


def test_eth_cohort_present_and_unique():
    cohort = generate_cohort()
    ids = [om.id for om in cohort]
    assert len(ids) == len(set(ids)), "duplicate strategy ids"
    assert EXPECTED_ETH_IDS <= set(ids), "ETH cohort missing (check max_strategies cap)"


def test_eth_cohort_is_single_asset_crypto():
    for om in generate_cohort():
        if om.id.endswith("-eth"):
            assert om.symbols == ["ETH/USD"]
            assert om.market == "crypto"
            assert om.meta.get("focus") == "ETH"


def test_cohort_respects_cap():
    cohort = generate_cohort()
    assert len(cohort) <= get_config().omnivore.max_strategies
