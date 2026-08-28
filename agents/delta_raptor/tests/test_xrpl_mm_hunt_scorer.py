"""Unit tests for the XRPL hunt scorer (opportunity-score pair ranking)."""
import pytest

from agents.delta_raptor.routines.xrpl_mm_hunt_scorer import (
    Config,
    PairSignal,
    _fill_rate,
    _volume_boost,
    _volume_weight,
    allocate_capital,
    decide_rotation,
    pair_score,
    rank_pairs,
)

RLUSD = PairSignal(pair="RLUSD-XRP", viable=True, book_ok=True, headroom_bps=19.0, book_spread_bps=11.0, fills_window=30, window_sec=1800)
USDC = PairSignal(pair="USDC-RLUSD", viable=True, book_ok=True, headroom_bps=13.0, book_spread_bps=1.0, fills_window=5, window_sec=1800)
BBRL = PairSignal(pair="BBRL-RLUSD", viable=True, book_ok=True, headroom_bps=50.0, book_spread_bps=300.0, fills_window=2, window_sec=1800)
DEAD = PairSignal(pair="EUROP-XRP", viable=False, book_ok=True, headroom_bps=90.0, book_spread_bps=2.0, fills_window=0, window_sec=1800)
NOBOOK = PairSignal(pair="XRP-USDC", viable=True, book_ok=False, headroom_bps=30.0, book_spread_bps=None, fills_window=0, window_sec=1800)


class TestFillRate:
    def test_prior_keeps_new_pairs_scored(self):
        assert _fill_rate(0, 1800, 0.5) == pytest.approx(0.5 / 1800)
        assert _fill_rate(30, 1800, 0.5) == pytest.approx(30.5 / 1800)

    def test_window_guard(self):
        assert _fill_rate(0, 0, 0.5) == pytest.approx(0.5)


class TestVolumeBoost:
    def test_no_boost_at_zero(self):
        assert _volume_boost(0.0, 1.0) == 1.0

    def test_surge_boosts_linearly(self):
        assert _volume_boost(100.0, 1.0) == pytest.approx(2.0)  # +100% -> x2
        assert _volume_boost(200.0, 1.0) == pytest.approx(3.0)  # +200% -> x3

    def test_negative_volume_never_penalizes(self):
        assert _volume_boost(-50.0, 1.0) == 1.0
        assert _volume_boost(-500.0, 3.0) == 1.0

    def test_weight_scales(self):
        assert _volume_boost(100.0, 2.0) == pytest.approx(3.0)  # weight 2 -> x3
        assert _volume_boost(100.0, 0.0) == 1.0  # weight 0 -> no effect


class TestPairScore:
    def test_non_viable_scores_zero(self):
        assert pair_score(DEAD, 1 / 60, 0.5) == 0.0

    def test_missing_book_scores_zero(self):
        assert pair_score(NOBOOK, 1 / 60, 0.5) == 0.0

    def test_more_headroom_scores_higher(self):
        a = pair_score(PairSignal(pair="A", viable=True, book_ok=True, headroom_bps=20.0, book_spread_bps=10.0, fills_window=10, window_sec=1800), 1 / 60, 0.5)
        b = pair_score(PairSignal(pair="B", viable=True, book_ok=True, headroom_bps=10.0, book_spread_bps=10.0, fills_window=10, window_sec=1800), 1 / 60, 0.5)
        assert a > b > 0

    def test_wider_book_scores_worse(self):
        a = pair_score(PairSignal(pair="A", viable=True, book_ok=True, headroom_bps=20.0, book_spread_bps=10.0, fills_window=10, window_sec=1800), 1 / 60, 0.5)
        b = pair_score(PairSignal(pair="B", viable=True, book_ok=True, headroom_bps=20.0, book_spread_bps=100.0, fills_window=10, window_sec=1800), 1 / 60, 0.5)
        assert a > b

    def test_volume_surge_boosts_score(self):
        calm = PairSignal(pair="C", viable=True, book_ok=True, headroom_bps=10.0, book_spread_bps=10.0, fills_window=30, window_sec=1800)
        hot = PairSignal(pair="H", viable=True, book_ok=True, headroom_bps=10.0, book_spread_bps=10.0, fills_window=30, window_sec=1800, volume_change_pct=200.0)
        assert pair_score(hot, 1 / 60, 0.5) == pytest.approx(pair_score(calm, 1 / 60, 0.5) * 3.0, rel=1e-9)

    def test_fill_rate_boost_capped(self):
        # Both pairs exceed the 3x cap -> identical scores (cap, not raw rate)
        hot = PairSignal(pair="H", viable=True, book_ok=True, headroom_bps=10.0, book_spread_bps=10.0, fills_window=10_000, window_sec=1800)
        also_hot = PairSignal(pair="C", viable=True, book_ok=True, headroom_bps=10.0, book_spread_bps=10.0, fills_window=5_400, window_sec=1800)
        assert pair_score(hot, 1 / 60, 0.5) == pytest.approx(pair_score(also_hot, 1 / 60, 0.5), rel=1e-9)
        # and both cap at 3x a reference-rate pair (29 fills ~ 1/min with prior)
        ref = PairSignal(pair="R", viable=True, book_ok=True, headroom_bps=10.0, book_spread_bps=10.0, fills_window=29, window_sec=1800)
        assert pair_score(hot, 1 / 60, 0.5) == pytest.approx(pair_score(ref, 1 / 60, 0.5) * 3.0, abs=0.05)


class TestRankPairs:
    def test_orders_by_score(self):
        ranked = rank_pairs([BBRL, USDC, RLUSD, DEAD, NOBOOK])
        assert ranked[0]["pair"] == ranked[0]["pair"]  # sanity
        scores = [r["score"] for r in ranked]
        assert scores == sorted(scores, reverse=True)
        assert all(r["score"] > 0 for r in ranked[:3])
        # dead + nobook excluded from huntable (score 0, ranked last)
        assert ranked[-1]["score"] == 0.0 and ranked[-2]["score"] == 0.0


class TestDecideRotation:
    def test_hold_when_nothing_beats_core(self):
        ranked = rank_pairs([RLUSD, USDC])
        action, target, _ = decide_rotation(ranked, "RLUSD-XRP", beat_threshold=10.0)
        assert action == "HOLD" and target is None

    def test_rotate_when_pair_beats_core(self):
        # USDC-RLUSD: tight book (1 bps) + headroom -> high score
        ranked = rank_pairs([RLUSD, USDC])
        action, target, _ = decide_rotation(ranked, "RLUSD-XRP", beat_threshold=1.2)
        assert action == "ROTATE_SLICE"
        assert target == "USDC-RLUSD"

    def test_hold_when_core_missing(self):
        ranked = rank_pairs([USDC])
        action, target, _ = decide_rotation(ranked, "RLUSD-XRP")
        assert action == "HOLD" and target is None

    def test_hold_when_core_scores_zero(self):
        dead_core = PairSignal(pair="RLUSD-XRP", viable=False, book_ok=False, headroom_bps=0.0, book_spread_bps=None, fills_window=0, window_sec=1800)
        ranked = rank_pairs([dead_core, USDC])
        action, target, _ = decide_rotation(ranked, "RLUSD-XRP")
        assert action == "HOLD" and target is None


class TestAllocateCapital:
    def _signal(self, pair, vol=0.0):
        return PairSignal(pair=pair, viable=True, book_ok=True, headroom_bps=15.0,
                          book_spread_bps=10.0, fills_window=10, window_sec=1800,
                          volume_change_pct=vol)

    def _scored(self, *pairs):
        return rank_pairs(list(pairs))

    def test_parks_on_core_when_no_surge(self):
        scored = self._scored(self._signal("RLUSD-XRP"), self._signal("XRP-USDC"))
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, 800.0)}
        assert alloc == {"RLUSD-XRP": 800.0, "XRP-USDC": 0.0}

    def test_full_shift_on_dominance(self):
        # A surges +400 (weight 450) vs B 0 (weight 50): 450 >= 3x50 -> ALL to A
        scored = self._scored(self._signal("RLUSD-XRP", vol=400.0), self._signal("XRP-USDC"))
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, 800.0)}
        assert alloc == {"RLUSD-XRP": 800.0, "XRP-USDC": 0.0}

    def test_no_split_without_dominance(self):
        # A +80 (130) vs B 0 (50): 130 < 3x50 -> stay 100% on core, no drip to XRP-USDC
        scored = self._scored(self._signal("RLUSD-XRP", vol=80.0), self._signal("XRP-USDC"))
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, 800.0)}
        assert alloc == {"RLUSD-XRP": 800.0, "XRP-USDC": 0.0}

    def test_exactly_three_x_is_full_shift(self):
        # XRP-USDC +100 (150) vs core 0 (50): 150 >= 3x50 -> fly to XRP-USDC
        scored = self._scored(self._signal("RLUSD-XRP"), self._signal("XRP-USDC", vol=100.0))
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, 800.0)}
        assert alloc == {"RLUSD-XRP": 0.0, "XRP-USDC": 800.0}

    def test_startup_never_funds_xrp_usdc(self):
        scored = self._scored(self._signal("RLUSD-XRP"), self._signal("XRP-USDC"), self._signal("USDC-RLUSD"))
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, 800.0)}
        assert alloc["RLUSD-XRP"] == 800.0
        assert alloc["XRP-USDC"] == 0.0
        assert alloc["USDC-RLUSD"] == 0.0

    def test_non_huntable_pair_gets_zero(self):
        dead = PairSignal(pair="DEAD", viable=False, book_ok=True, headroom_bps=90.0,
                          book_spread_bps=2.0, fills_window=0, window_sec=1800)
        scored = self._scored(self._signal("RLUSD-XRP"), dead)
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, 800.0)}
        assert alloc["DEAD"] == 0.0
        assert alloc["RLUSD-XRP"] == 800.0

    def test_rotation_own_surge_hops_even_when_core_also_printed(self):
        scored = self._scored(self._signal("RLUSD-XRP", vol=20.0), self._signal("XRP-USDC", vol=60.0))
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, 800.0)}
        assert alloc == {"RLUSD-XRP": 0.0, "XRP-USDC": 800.0}

    def test_below_curious_stays_core(self):
        scored = self._scored(self._signal("RLUSD-XRP"), self._signal("XRP-USDC", vol=40.0))
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, 800.0)}
        assert alloc == {"RLUSD-XRP": 800.0, "XRP-USDC": 0.0}

    def test_harvest_hop_moves_toehold_not_wallet(self):
        scored = self._scored(self._signal("RLUSD-XRP"), self._signal("XRP-USDC", vol=100.0))
        from agents.delta_raptor.routines._xrpl_mm_perch import compose_perch

        live = compose_perch("harvest", wallet=800, toehold=100).live
        alloc = {a["pair"]: a["capital_quote"] for a in allocate_capital(scored, live)}
        assert alloc == {"RLUSD-XRP": 0.0, "XRP-USDC": 100.0}

    def test_volume_weight_cap(self):
        assert _volume_weight(500.0, 50.0) == 450.0  # capped at 400 surge
        assert _volume_weight(-50.0, 50.0) == 50.0  # negative -> baseline only


class TestConfig:
    def test_defaults(self):
        c = Config(pairs=[RLUSD])
        assert c.core_pair == "RLUSD-XRP"
        assert c.hunt_beat_threshold == 1.2
        assert c.hunt_slice_pct == 0.15
        assert c.fill_rate_ref == pytest.approx(1 / 60)
        assert c.hunting_mode == "harvest"
        assert c.toehold_quote == 100.0
        assert c.curious_surge_pct == 50.0
