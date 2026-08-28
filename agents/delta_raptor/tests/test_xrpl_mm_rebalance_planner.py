"""Unit tests for the XRPL rebalance planner's pure math.

Covers: inventory state, band trigger, progressive sizing, AMM cost
(fee + constant-product impact), CLOB cross cost, venue selection + cost gate.
"""
import pytest

from agents.delta_raptor.routines.xrpl_mm_rebalance_planner import (
    _amm_swap_cost_bps,
    _choose_venue,
    _clob_cross_cost_bps,
    _inventory_state,
    _progressive_amount,
    _rebalance_signal,
)


class TestInventoryState:
    def test_balanced(self):
        s = _inventory_state(100.0, 100.0, 1.0)
        assert s["base_value"] == 100.0
        assert s["quote_value"] == 100.0
        assert s["base_share"] == 50.0

    def test_overweight_base(self):
        s = _inventory_state(300.0, 100.0, 1.0)
        assert s["base_share"] == pytest.approx(75.0)

    def test_zero_price_safe(self):
        s = _inventory_state(100.0, 100.0, 0.0)
        assert s["base_value"] == 0.0
        assert s["base_share"] == 0.0

    def test_zero_total_safe(self):
        s = _inventory_state(0.0, 0.0, 1.0)
        assert s["base_share"] == 0.0


class TestRebalanceSignal:
    def test_within_band_no_trigger(self):
        # deviation exactly at band edge does NOT trigger
        assert _rebalance_signal(65.0, 50.0, 15.0)["needed"] is False
        assert _rebalance_signal(35.0, 50.0, 15.0)["needed"] is False

    def test_overweight_triggers_sell(self):
        sig = _rebalance_signal(66.0, 50.0, 15.0)
        assert sig["needed"] is True
        assert sig["side"] == "SELL_BASE"
        assert sig["deviation"] == pytest.approx(16.0)

    def test_underweight_triggers_buy(self):
        sig = _rebalance_signal(34.0, 50.0, 15.0)
        assert sig["needed"] is True
        assert sig["side"] == "BUY_BASE"
        assert sig["deviation"] == pytest.approx(-16.0)


class TestProgressiveAmount:
    def test_half_step(self):
        assert _progressive_amount(100.0, 0.5, 50.0) == pytest.approx(50.0)

    def test_capped_by_max(self):
        assert _progressive_amount(100.0, 0.5, 30.0) == pytest.approx(30.0)

    def test_fraction_less_than_max(self):
        assert _progressive_amount(100.0, 0.25, 100.0) == pytest.approx(25.0)

    def test_zero_excess(self):
        assert _progressive_amount(0.0, 0.5, 50.0) == 0.0
        assert _progressive_amount(-5.0, 0.5, 50.0) == 0.0


class TestAmmSwapCostBps:
    def test_exact_constant_product_no_fee(self):
        # amount 10 into pool 1000/2000, fee 0: out = 2000*10/1010 = 19.80198
        # mid = 20 -> impact = (20-19.80198)/20 * 1e4 = 99.0099 bps
        cost = _amm_swap_cost_bps(0.0, 10.0, 1000.0, 2000.0)
        assert cost == pytest.approx(99.0099, rel=1e-3)

    def test_fee_adds_on_top(self):
        # 0.1% fee: cost = fee_bps + impact(on reduced input). Impact shifts by
        # a fraction of a bp because the fee shrinks the effective swap size.
        cost_fee = _amm_swap_cost_bps(0.001, 10.0, 1000.0, 2000.0)
        cost_zero = _amm_swap_cost_bps(0.0, 10.0, 1000.0, 2000.0)
        assert cost_fee == pytest.approx(cost_zero + 10.0, abs=0.2)
        assert cost_fee > cost_zero

    def test_bigger_swap_more_impact(self):
        small = _amm_swap_cost_bps(0.0, 10.0, 1000.0, 2000.0)
        big = _amm_swap_cost_bps(0.0, 100.0, 1000.0, 2000.0)
        assert big > small

    def test_invalid_inputs_infinite(self):
        assert _amm_swap_cost_bps(0.0, 0.0, 1000.0, 2000.0) == float("inf")
        assert _amm_swap_cost_bps(0.0, 10.0, 0.0, 2000.0) == float("inf")
        assert _amm_swap_cost_bps(0.0, 10.0, 1000.0, 0.0) == float("inf")

    def test_realistic_rlusd_pool_small_swap(self):
        # RLUSD-XRP pool ~1.99M/1.99M; $100 swap ≈ fee 20.8 bps + ~2 bps impact
        cost = _amm_swap_cost_bps(0.00208, 100.0, 1_993_231.0, 1_991_033.0)
        assert 20.0 < cost < 26.0


class TestClobCrossCostBps:
    def test_returns_book_spread(self):
        assert _clob_cross_cost_bps(11.2) == pytest.approx(11.2)

    def test_unknown_book_none(self):
        assert _clob_cross_cost_bps(None) is None


class TestChooseVenue:
    def test_picks_cheaper(self):
        assert _choose_venue(10.0, 30.0, 25.0) == ("CLOB", 10.0)
        assert _choose_venue(30.0, 10.0, 25.0) == ("AMM", 10.0)

    def test_gate_blocks_expensive(self):
        assert _choose_venue(10.0, 30.0, 5.0)[0] == "HOLD"

    def test_single_venue(self):
        assert _choose_venue(None, 20.0, 25.0) == ("AMM", 20.0)
        assert _choose_venue(20.0, None, 25.0) == ("CLOB", 20.0)
        assert _choose_venue(None, 30.0, 25.0) == ("HOLD", 30.0)

    def test_no_venues(self):
        assert _choose_venue(None, None, 25.0) == ("HOLD", None)

    def test_amm_wins_when_clob_unknown_and_under_gate(self):
        venue, cost = _choose_venue(None, 12.0, 25.0)
        assert venue == "AMM" and cost == 12.0
