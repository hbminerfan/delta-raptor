"""Harvest vs race perch — Cup ceilings, small-purse fit, SELL seed if XRP locked."""
import pytest

from agents.delta_raptor.routines._xrpl_mm_perch import (
    HARVEST,
    RACE,
    compose_perch,
    normalize_mode,
    xrpl_purse,
)


class TestNormalizeMode:
    def test_blank_is_harvest(self):
        assert normalize_mode(None) == HARVEST
        assert normalize_mode("") == HARVEST
        assert normalize_mode("harvest") == HARVEST

    def test_race_aliases(self):
        assert normalize_mode("race") == RACE
        assert normalize_mode("Mode-1") == RACE
        assert normalize_mode("incentivized") == RACE


class TestRace:
    def test_whole_envelope_both_sides(self):
        p = compose_perch("race", wallet=800, toehold=100)
        assert p.live == 800.0
        assert p.idle == 0.0
        assert p.buy_live == 400.0
        assert p.sell_live == 400.0
        assert p.levels_per_side == 3
        assert p.one_sided is False


class TestHarvest:
    def test_seed_is_toehold_buy_idle_off_book(self):
        p = compose_perch("harvest", wallet=800, toehold=100, inv_sellable_usd=0)
        assert p.live == 100.0
        assert p.idle == 700.0
        assert p.buy_live == 100.0
        assert p.sell_live == 0.0
        assert p.one_sided is True
        assert p.levels_per_side == 1
        assert p.hold is False

    def test_fill_unlocks_sell_from_inventory(self):
        p = compose_perch("harvest", wallet=800, toehold=100, inv_sellable_usd=100)
        assert p.buy_live == 100.0
        assert p.sell_live == 100.0
        assert p.idle == 700.0
        assert p.one_sided is False

    def test_spike_does_not_rest_the_envelope(self):
        p = compose_perch("harvest", wallet=800, toehold=100, inv_sellable_usd=0)
        assert p.live == pytest.approx(100.0)
        assert p.idle == pytest.approx(700.0)
        assert p.live + p.idle == pytest.approx(p.wallet)

    def test_small_purse_does_not_park_the_whole_book(self):
        p = compose_perch("harvest", wallet=80, toehold=100)
        assert p.live == pytest.approx(16.0)
        assert p.idle == pytest.approx(64.0)
        assert p.hold is False

    def test_organizer_rlusd_with_xrp_locked_sells_inventory(self):
        p = compose_perch(
            "harvest",
            wallet=86.0,
            toehold=100,
            buy_room_usd=0.0,
            sell_room_usd=67.6,
        )
        assert p.hold is False
        assert p.buy_live == 0.0
        assert p.sell_live == pytest.approx(17.2)
        assert p.live == pytest.approx(17.2)
        assert "SELL" in p.note

    def test_too_small_holds(self):
        p = compose_perch("harvest", wallet=8, toehold=100, buy_room_usd=0, sell_room_usd=0)
        assert p.hold is True
        assert p.live == 0.0

    def test_hunt_moves_live_not_idle(self):
        p = compose_perch("harvest", wallet=800, toehold=100)
        assert p.live == 100.0
        assert p.live != p.wallet


class TestXrplPurse:
    def test_locked_xrp_zero_buy_room(self):
        rows = [
            {"token": "XRP", "units": 13.0, "available_units": -5.6, "value": 18.2, "price": 1.4},
            {"token": "RLUSD", "units": 67.6, "available_units": 67.6, "value": 67.6, "price": 1.0},
        ]
        p = xrpl_purse(rows, xrp_usd=1.4, reserve_xrp=1.2)
        assert p["buy_room_usd"] == 0.0
        assert p["sell_room_usd"] == pytest.approx(67.6, rel=0.02)
        assert p["wallet_usd"] == pytest.approx(85.8, rel=0.02)


class TestHarvestCompetitionUnstick:
    """Competition / SIT must quote when toehold fits — frac must not permanent-HOLD."""

    def test_competition_dual_arm_toehold_not_stuck(self):
        # observed XRPL bag large, sleeve $240, toehold $80
        p = compose_perch(
            "harvest",
            wallet=850.0,
            toehold=80.0,
            envelope=240.0,
            buy_room_usd=26.0,
            sell_room_usd=830.0,
        )
        assert p.hold is False
        assert p.live == pytest.approx(80.0)
        assert p.buy_live == pytest.approx(26.0) or p.buy_live == pytest.approx(80.0) or p.live >= 5

    def test_competition_live_capped_by_buy_room_still_quotes(self):
        p = compose_perch(
            "harvest",
            wallet=850.0,
            toehold=80.0,
            envelope=240.0,
            buy_room_usd=26.0,
            sell_room_usd=830.0,
        )
        assert p.hold is False
        assert p.live >= 5.0
        assert p.buy_live == pytest.approx(26.0)
        # sell can also be live-sized
        assert p.sell_live >= 5.0

    def test_sleeve_as_observed_still_arms_toehold(self):
        # Agent mistake: observed_wallet_usd == sleeve == 240, toehold 80
        p = compose_perch(
            "harvest",
            wallet=240.0,
            toehold=80.0,
            envelope=240.0,
            buy_room_usd=26.0,
            sell_room_usd=830.0,
        )
        assert p.hold is False
        assert p.live >= 5.0

    def test_sit_ten_dollar_sleeve_not_stuck(self):
        # SIT $10 envelope used to do live=min(10, 10*0.2)=2 < min_live → HOLD forever
        p = compose_perch(
            "harvest",
            wallet=850.0,
            toehold=10.0,
            envelope=10.0,
            buy_room_usd=26.0,
            sell_room_usd=830.0,
        )
        assert p.hold is False
        assert p.live == pytest.approx(10.0)

    def test_sit_sleeve_passed_as_observed_not_stuck(self):
        p = compose_perch(
            "harvest",
            wallet=10.0,
            toehold=10.0,
            envelope=10.0,
            buy_room_usd=26.0,
            sell_room_usd=830.0,
        )
        assert p.hold is False
        assert p.live == pytest.approx(10.0)
