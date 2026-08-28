"""Nest hop + surge picker — curious volume, then home, with a whitelist bridge."""
import pytest

from agents.delta_raptor.routines._xrpl_mm_hop import nest_tokens, pick_nest, plan_hop


class TestPlanHop:
    def test_rlusd_xrp_to_xrp_usdc_buys_usdc_on_usdc_rlusd(self):
        h = plan_hop("RLUSD-XRP", "XRP-USDC")
        assert h is not None
        assert h.bridge_pair == "USDC-RLUSD"
        assert h.pay_asset == "RLUSD"
        assert h.receive_asset == "USDC"
        assert h.side == "BUY"  # USDC-RLUSD: BUY base USDC, pay quote RLUSD

    def test_home_from_xrp_usdc_sells_usdc_for_rlusd(self):
        h = plan_hop("XRP-USDC", "RLUSD-XRP")
        assert h is not None
        assert h.bridge_pair == "USDC-RLUSD"
        assert h.pay_asset == "USDC"
        assert h.receive_asset == "RLUSD"
        assert h.side == "SELL"

    def test_same_nest_needs_no_bridge(self):
        assert plan_hop("RLUSD-XRP", "RLUSD-XRP") is None
        assert nest_tokens("RLUSD-XRP") == nest_tokens("XRP-RLUSD")

    def test_no_path_when_bridge_missing(self):
        assert plan_hop("RLUSD-XRP", "BBRL-RLUSD") is None

    def test_europ_xrp_from_core_uses_europ_rlusd(self):
        h = plan_hop("RLUSD-XRP", "EUROP-XRP")
        assert h is not None
        assert h.bridge_pair == "EUROP-RLUSD"
        assert h.pay_asset == "RLUSD"
        assert h.receive_asset == "EUROP"
        assert h.side == "BUY"


class TestPickNest:
    def _row(self, pair, vol, score=10.0):
        return {"pair": pair, "score": score, "volume_change_pct": vol}

    def test_core_wins_when_nobody_surged(self):
        nest = pick_nest(
            [self._row("RLUSD-XRP", 5.0), self._row("XRP-USDC", 10.0)],
            curious_surge_pct=50.0,
        )
        assert nest == "RLUSD-XRP"

    def test_rotation_surge_beats_core_even_if_core_is_the_biggest_book(self):
        nest = pick_nest(
            [self._row("RLUSD-XRP", 8.0), self._row("XRP-USDC", 60.0)],
            curious_surge_pct=50.0,
        )
        assert nest == "XRP-USDC"

    def test_below_threshold_stays_home(self):
        nest = pick_nest(
            [self._row("RLUSD-XRP", 0.0), self._row("XRP-USDC", 40.0)],
            curious_surge_pct=50.0,
        )
        assert nest == "RLUSD-XRP"

    def test_fade_returns_home(self):
        nest = pick_nest(
            [self._row("RLUSD-XRP", 2.0), self._row("XRP-USDC", 12.0)],
            curious_surge_pct=50.0,
        )
        assert nest == "RLUSD-XRP"

    def test_hottest_rotation_wins(self):
        nest = pick_nest(
            [
                self._row("RLUSD-XRP", 0.0),
                self._row("XRP-USDC", 55.0),
                self._row("EUROP-XRP", 120.0),
            ],
            curious_surge_pct=50.0,
        )
        assert nest == "EUROP-XRP"

    def test_dead_score_cannot_host(self):
        nest = pick_nest(
            [
                self._row("RLUSD-XRP", 0.0, score=10.0),
                self._row("XRP-USDC", 200.0, score=0.0),
            ]
        )
        assert nest == "RLUSD-XRP"

    def test_skips_nest_with_no_bridge(self):
        nest = pick_nest(
            [
                self._row("RLUSD-XRP", 0.0),
                self._row("BBRL-RLUSD", 200.0),
                self._row("XRP-USDC", 60.0),
            ],
            parked_pair="RLUSD-XRP",
        )
        assert nest == "XRP-USDC"
