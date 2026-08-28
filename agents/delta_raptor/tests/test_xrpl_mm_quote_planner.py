"""Unit tests for the XRPL maker quote planner's pure math.

Covers the deterministic helpers only (no API, no network):
- _hour_mult_span: duration-weighted intraday volatility clock
- _realized_vol_per_sec: per-second std dev of log returns
- _extract_ref_price: defensive CEX reference price parsing
- Config model defaults and constants

The network-facing parts (reference fetch, amm_info, XRPL book) are covered by
the read-only integration check instead of mocks.
"""
import math
from datetime import datetime, timezone

import pytest

from agents.delta_raptor.routines.xrpl_mm_quote_planner import (
    BASE_RESERVE_XRP,
    HOUR_VOL_MULT,
    OWNER_RESERVE_XRP,
    Config,
    _amm_info_assets,
    _extract_ref_price,
    _hour_mult_span,
    _issue_currency_code,
    _realized_vol_per_sec,
    _top_of_book_anchor,
    _widen_quotes,
)

RLUSD_ISS = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
USDC_ISS = "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE"
RLUSD_HEX = "524C555344000000000000000000000000000000"
USDC_HEX = "5553444300000000000000000000000000000000"

MS = 3_600_000  # ms per hour


def _utc_ms(y=2026, m=1, d=1, hour=0, minute=0):
    return int(datetime(y, m, d, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


# ── _hour_mult_span ─────────────────────────────────────────────────────────


class TestHourMultSpan:
    def test_end_le_start_uses_current_hour(self):
        # epoch 0 is 1970-01-01 00:00 UTC → hour 0 → 1.03
        assert _hour_mult_span(0, 0) == HOUR_VOL_MULT[0]
        assert _hour_mult_span(500, 100) == HOUR_VOL_MULT[0]

    def test_single_hour_span_returns_that_hour(self):
        start = _utc_ms(hour=14)  # 14:00 UTC → peak 1.50
        assert _hour_mult_span(start, start + 30 * 60_000) == pytest.approx(1.50)

    def test_multi_hour_span_is_duration_weighted(self):
        # 13:00-14:00 (1.28) + 14:00-15:00 (1.50) = mean 1.39
        start = _utc_ms(hour=13)
        end = _utc_ms(hour=15)
        assert _hour_mult_span(start, end) == pytest.approx(1.39)

    def test_partial_hour_weighting(self):
        # 13:00-13:30: half at 1.28, then 13:30-14:00: half at 1.28 → 1.28.
        # Extend to 14:30: 1h at 1.28 + 0.5h at 1.50 → (1.28 + 0.75)/1.5 = 1.3533
        start = _utc_ms(hour=13)
        end = _utc_ms(hour=14, minute=30)
        assert _hour_mult_span(start, end) == pytest.approx((1.28 + 0.75) / 1.5)

    def test_span_never_zero_for_valid_hours(self):
        # 24h window must equal the full-day average
        start = _utc_ms(hour=0)
        end = _utc_ms(hour=0) + 24 * MS
        expected = sum(HOUR_VOL_MULT[h] for h in range(24)) / 24
        assert _hour_mult_span(start, end) == pytest.approx(expected)


# ── _realized_vol_per_sec ────────────────────────────────────────────────────


class TestRealizedVolPerSec:
    def test_too_few_closes_returns_zero(self):
        assert _realized_vol_per_sec([], 60) == 0.0
        assert _realized_vol_per_sec([100.0], 60) == 0.0
        assert _realized_vol_per_sec([100.0, 110.0], 60) == 0.0  # needs ≥3

    def test_flat_series_returns_zero(self):
        assert _realized_vol_per_sec([100.0, 100.0, 100.0, 100.0], 60) == 0.0

    def test_known_two_return_series(self):
        closes = [100.0, 120.0, 90.0]
        r1, r2 = math.log(1.2), math.log(0.75)
        mean = (r1 + r2) / 2
        var = ((r1 - mean) ** 2 + (r2 - mean) ** 2) / 2
        expected = math.sqrt(var / 60)
        assert _realized_vol_per_sec(closes, 60) == pytest.approx(expected, rel=1e-9)

    def test_interval_scaling(self):
        closes = [100.0, 120.0, 90.0]
        per60 = _realized_vol_per_sec(closes, 60)
        per240 = _realized_vol_per_sec(closes, 240)
        # sqrt(60/240) = 0.5 → per240 == per60 / 2
        assert per240 == pytest.approx(per60 / 2, rel=1e-9)

    def test_nonpositive_closes_skipped(self):
        closes = [100.0, 0.0, -5.0, 120.0, 90.0]
        expected = _realized_vol_per_sec([100.0, 120.0, 90.0], 60)
        assert _realized_vol_per_sec(closes, 60) == pytest.approx(expected, rel=1e-9)

    def test_bad_bar_preserves_chain(self):
        # A zero bar between positives must NOT drop the spanning return —
        # regression for the adjacent-pair filter bug (vol collapsing to 0).
        closes = [100.0, 0.0, 120.0, 90.0]
        expected = _realized_vol_per_sec([100.0, 120.0, 90.0], 60)
        assert _realized_vol_per_sec(closes, 60) == pytest.approx(expected, rel=1e-9)

    def test_all_nonpositive_returns_zero(self):
        assert _realized_vol_per_sec([0.0, 0.0, -1.0], 60) == 0.0


# ── _extract_ref_price ───────────────────────────────────────────────────────


class TestExtractRefPrice:
    FALLBACK = 9.99

    def test_direct_flat_payload(self):
        assert _extract_ref_price({"XRP-USDT": "0.54321"}, "XRP-USDT", self.FALLBACK) == pytest.approx(0.54321)

    def test_nested_per_connector_payload(self):
        raw = {"bitget_perpetual": {"XRP-USDT": 0.54321}}
        assert _extract_ref_price(raw, "XRP-USDT", self.FALLBACK) == pytest.approx(0.54321)

    def test_nested_wrong_pair_falls_back(self):
        raw = {"bitget_perpetual": {"BTC-USDT": 1.0}}
        assert _extract_ref_price(raw, "XRP-USDT", self.FALLBACK) == self.FALLBACK

    def test_non_numeric_values_skipped_not_raised(self):
        raw = {"connector_names": ["bitget_perpetual", "xrpl"], "XRP-USDT": "abc"}
        assert _extract_ref_price(raw, "XRP-USDT", self.FALLBACK) == self.FALLBACK

    def test_negative_or_zero_price_rejected(self):
        assert _extract_ref_price({"XRP-USDT": "-0.5"}, "XRP-USDT", self.FALLBACK) == self.FALLBACK
        assert _extract_ref_price({"XRP-USDT": 0}, "XRP-USDT", self.FALLBACK) == self.FALLBACK

    def test_non_dict_inputs_fall_back(self):
        assert _extract_ref_price(None, "XRP-USDT", self.FALLBACK) == self.FALLBACK
        assert _extract_ref_price(["x"], "XRP-USDT", self.FALLBACK) == self.FALLBACK
        assert _extract_ref_price(3.14, "XRP-USDT", self.FALLBACK) == self.FALLBACK

    def test_direct_beats_nested(self):
        raw = {"XRP-USDT": 0.9, "bitget_perpetual": {"XRP-USDT": 0.5}}
        assert _extract_ref_price(raw, "XRP-USDT", self.FALLBACK) == pytest.approx(0.9)


# ── _issue_currency_code ─────────────────────────────────────────────────────


class TestIssueCurrencyCode:
    def test_three_char_ascii_passes_through(self):
        assert _issue_currency_code("USD") == "USD"
        assert _issue_currency_code("XRP") == "XRP"

    def test_long_currency_becomes_hex(self):
        # Regression: rippled rejects ASCII >3 chars with issueMalformed, so
        # RLUSD must go out as the 40-char hex form.
        assert _issue_currency_code("RLUSD") == "524C555344000000000000000000000000000000"
        assert len(_issue_currency_code("EUROP")) == 40
        assert _issue_currency_code("EUROP") == "4555524F50000000000000000000000000000000"

    def test_already_hex_passes_through(self):
        assert (
            _issue_currency_code("524C555344000000000000000000000000000000")
            == "524C555344000000000000000000000000000000"
        )

    def test_lowercase_hex_preserved(self):
        assert _issue_currency_code("524c555344000000000000000000000000000000") == (
            "524c555344000000000000000000000000000000"
        )


# ── _widen_quotes ────────────────────────────────────────────────────────────


class TestWidenQuotes:
    def test_one_percent_each_side(self):
        assert _widen_quotes(1.0, 1.0) == pytest.approx((0.99, 1.01))

    def test_point_one_percent_each_side(self):
        assert _widen_quotes(1.0, 0.1) == pytest.approx((0.999, 1.001))

    def test_zero_distance_is_mid(self):
        assert _widen_quotes(1.0, 0.0) == pytest.approx((1.0, 1.0))

    def test_negative_distance_abs(self):
        assert _widen_quotes(1.0, -1.0) == pytest.approx((0.99, 1.01))

    def test_realistic_mid(self):
        bid, ask = _widen_quotes(0.997606, 1.0)
        assert bid == pytest.approx(0.987630, rel=1e-5)
        assert ask == pytest.approx(1.007582, rel=1e-5)
        assert (ask - bid) / bid * 100 == pytest.approx(2.02, rel=1e-2)  # ~2% total


# ── _top_of_book_anchor ──────────────────────────────────────────────────────


class TestTopOfBookAnchor:
    def test_improves_both_sides(self):
        # best bid 1.0002, best ask 1.0013, improve 0.01% -> new best both sides
        bid, ask = _top_of_book_anchor(1.0002, 1.0013, 0.01)
        assert bid == pytest.approx(1.0002 * 1.0001)
        assert ask == pytest.approx(1.0013 * 0.9999)
        assert bid > 1.0002  # pays a hair more
        assert ask < 1.0013  # asks a hair less

    def test_zero_improve_is_noop(self):
        assert _top_of_book_anchor(1.0, 1.1, 0.0) == pytest.approx((1.0, 1.1))

    def test_negative_improve_abs(self):
        assert _top_of_book_anchor(1.0, 1.1, -0.01) == pytest.approx((1.0 * 1.0001, 1.1 * 0.9999))

    def test_wide_book_still_topped(self):
        # XRP-USDC-style wide book: improvement is tiny relative to the spread
        bid, ask = _top_of_book_anchor(0.90, 1.10, 0.01)
        assert bid == pytest.approx(0.90009)
        assert ask == pytest.approx(1.09989)
        assert bid < ask  # never crosses the book


# ── _amm_info_assets ─────────────────────────────────────────────────────────


class TestAmmInfoAssets:
    def test_xrp_quoted_pair(self):
        # RLUSD-XRP: asset = quote (XRP, no issuer), asset2 = base (RLUSD + issuer)
        assets = _amm_info_assets("RLUSD-XRP", "", RLUSD_ISS)
        assert assets == ({"currency": "XRP"},
                          {"currency": RLUSD_HEX, "issuer": RLUSD_ISS})

    def test_rlusd_quoted_pair(self):
        # USDC-RLUSD: asset = quote (RLUSD + issuer), asset2 = base (USDC + issuer)
        assets = _amm_info_assets("USDC-RLUSD", RLUSD_ISS, USDC_ISS)
        assert assets == ({"currency": RLUSD_HEX, "issuer": RLUSD_ISS},
                          {"currency": USDC_HEX, "issuer": USDC_ISS})

    def test_issued_quote_against_xrp_base(self):
        # XRP-USDC: asset = quote (USDC + issuer), asset2 = XRP (no issuer)
        assets = _amm_info_assets("XRP-USDC", USDC_ISS, "")
        assert assets == ({"currency": USDC_HEX, "issuer": USDC_ISS},
                          {"currency": "XRP"})

    def test_missing_issuer_returns_none(self):
        assert _amm_info_assets("USDC-RLUSD", "", USDC_ISS) is None  # quote issuer missing
        assert _amm_info_assets("USDC-RLUSD", RLUSD_ISS, "") is None  # base issuer missing

    def test_malformed_pair_returns_none(self):
        assert _amm_info_assets("", RLUSD_ISS, USDC_ISS) is None
        assert _amm_info_assets("RLUSD", RLUSD_ISS, USDC_ISS) is None


# ── Config model ─────────────────────────────────────────────────────────────


class TestConfig:
    def test_defaults(self):
        c = Config()
        assert c.xrpl_pair == "RLUSD-XRP"
        assert c.reference_connector == "binance_perpetual"
        assert c.reference_pair == "XRP-USDT"
        assert c.tick_interval_sec == 300
        assert c.requote_interval_sec == 0  # unset → caller decides exposure window
        assert c.levels_per_side == 3
        assert c.total_amount_quote == 100.0
        assert c.adverse_k == 1.0
        assert c.use_vol_clock is True
        assert c.amm_fee_pct_fallback == 0.1
        assert c.amm_asset2_issuer == ""
        assert c.amm_asset2_currency == "RLUSD"
        assert c.hunting_mode == "harvest"
        assert c.wallet_ceiling_quote == 800.0
        assert c.toehold_quote == 100.0

    def test_requote_override(self):
        c = Config(requote_interval_sec=30)
        assert c.requote_interval_sec == 30


# ── Constants ────────────────────────────────────────────────────────────────


class TestConstants:
    def test_reserve_schedule(self):
        assert BASE_RESERVE_XRP == 1.0
        assert OWNER_RESERVE_XRP == 0.2

    def test_vol_clock_coverage_and_shape(self):
        assert len(HOUR_VOL_MULT) == 24
        assert all(0 <= h <= 23 for h in HOUR_VOL_MULT)
        assert all(v > 0 for v in HOUR_VOL_MULT.values())
        # Trough (0.78 at hour 10) < mean < peak (1.50 at hour 14)
        vals = list(HOUR_VOL_MULT.values())
        assert min(vals) < 1.0 < max(vals)
