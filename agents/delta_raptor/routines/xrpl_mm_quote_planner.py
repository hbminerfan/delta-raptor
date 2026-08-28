"""Plan XRPL CLOB maker quotes: reference fair value, spread bounds, viability verdict."""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone

import httpx
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client

from agents.delta_raptor.routines._xrpl_mm_perch import compose_perch, normalize_mode, xrpl_purse
from agents.delta_raptor.routines._xrpl_mm_sheet import mark_of, pin_nest_sheet, split_kv

logger = logging.getLogger(__name__)

CATEGORY = "Analysis"

# Public XRPL JSON-RPC. Used only for amm_info (pool trading fee), which sets the
# spread ceiling. Falls back to the configured value when unreachable.
XRPL_RPC = "https://xrplcluster.com/"

# XRPL reserve schedule (validator-voted; verify if the ledger amends it).
BASE_RESERVE_XRP = 1.0
OWNER_RESERVE_XRP = 0.2

# Intraday volatility clock. Split-half persistence r=0.90 across XRP/BTC/ETH/SOL,
# 733 days of Binance perp 1H bars. Values are vol relative to the daily average.
# Trough 04:00-11:00 UTC (Asia afternoon / pre-Europe); peak 13:00-15:00 UTC (US open).
HOUR_VOL_MULT = {
    0: 1.03, 1: 1.10, 2: 0.95, 3: 0.89, 4: 0.82, 5: 0.85,
    6: 0.82, 7: 0.82, 8: 0.91, 9: 0.83, 10: 0.78, 11: 0.82,
    12: 0.92, 13: 1.28, 14: 1.50, 15: 1.40, 16: 1.16, 17: 1.21,
    18: 1.07, 19: 1.04, 20: 1.02, 21: 0.97, 22: 0.97, 23: 0.82,
}
MS_PER_HOUR = 3_600_000


class Config(BaseModel):
    """Plan maker quotes for an XRPL CLOB pair against a CEX reference price."""

    xrpl_pair: str = Field(default="RLUSD-XRP", description="Pair as the xrpl connector names it")
    reference_connector: str = Field(
        default="binance_perpetual", description="CEX connector supplying fair value (Binance XRP-USDT perp)"
    )
    reference_pair: str = Field(default="XRP-USDT", description="Reference pair for XRP/USD")
    tick_interval_sec: int = Field(default=300, description="Seconds between LLM ticks")
    requote_interval_sec: int = Field(
        default=0,
        description="Actual seconds a quote stays live; 0 = same as tick_interval_sec. "
        "In controller mode this is the controller's executor_refresh_time, NOT the LLM "
        "tick — the bot requotes without the agent, so the LLM tick must not set the floor",
    )
    levels_per_side: int = Field(default=3, description="Quote levels per side")
    total_amount_quote: float = Field(default=100.0, description="Capital to deploy, USD")
    adverse_k: float = Field(
        default=1.0, description="Multiplier on expected adverse move; higher = wider floor"
    )
    use_vol_clock: bool = Field(
        default=True,
        description="Deseasonalise/re-seasonalise realized vol via the intraday clock",
    )
    amm_fee_pct_fallback: float = Field(
        default=0.1, description="Assumed AMM fee %% if amm_info is unreachable"
    )
    amm_asset2_issuer: str = Field(
        default="", description="Issuer address of the BASE asset (blank = skip amm_info)"
    )
    amm_asset2_currency: str = Field(default="RLUSD", description="Non-XRP asset currency code (legacy fallback)")
    amm_quote_issuer: str = Field(
        default="", description="Issuer of the QUOTE asset when it is not XRP (e.g. RLUSD for USDC-RLUSD)"
    )
    widen_enabled: bool = Field(
        default=True,
        description="When not viable (floor >= ceiling), keep quoting at a WIDENED spread instead of stopping",
    )
    widen_distance_pct: float = Field(
        default=1.0, description="Per-side distance from mid (percent) for the widened quote mode"
    )
    quote_mode: str = Field(
        default="top_of_book",
        description="'top_of_book' = improve the current best bid/ask by top_of_book_improve_pct; "
        "'reference' = spread from the CEX reference mid",
    )
    top_of_book_improve_pct: float = Field(
        default=0.01,
        description="Percent better than the current best price for the first quote level "
        "(0.01 = 0.01% tighter than best bid/ask -> new best orders both sides)",
    )
    hunting_mode: str = Field(
        default="harvest",
        description="race = Mode 1 whole envelope; harvest = Mode 2 toehold + idle cash",
    )
    wallet_ceiling_quote: float = Field(
        default=800.0, description="Wallet ceiling in USD. Not live book size in harvest."
    )
    toehold_quote: float = Field(
        default=100.0, description="Harvest live USD. Spike widens this, not the wallet."
    )
    inv_sellable_usd: float = Field(
        default=0.0,
        description="USD mark of inventory that can be sold (unlocks harvest SELL). "
        "0 = read the XRPL purse when a client is available.",
    )
    buy_room_usd: float = Field(
        default=-1.0,
        description="Free quote-side USD after reserve. -1 = read XRPL available XRP.",
    )
    observed_wallet_usd: float = Field(
        default=-1.0,
        description="Live XRPL mark. -1 = read portfolio. Cup 800 is a ceiling, not a floor.",
    )


# ── helpers ──────────────────────────────────────────────────────────────────


def _hour_mult_span(start_ms: int, end_ms: int) -> float:
    """Duration-weighted mean of HOUR_VOL_MULT over [start_ms, end_ms).

    Averaging across the span matters: a realized-vol lookback covers several hours,
    and a long tick interval means the quote stays live across several more. Using
    only the current hour would misprice both ends.
    """
    if end_ms <= start_ms:
        hour = datetime.fromtimestamp(start_ms / 1000, timezone.utc).hour
        return HOUR_VOL_MULT.get(hour, 1.0)
    total = weight = 0.0
    cursor = start_ms
    while cursor < end_ms:
        hour = datetime.fromtimestamp(cursor / 1000, timezone.utc).hour
        seg_end = min((cursor // MS_PER_HOUR + 1) * MS_PER_HOUR, end_ms)
        w = seg_end - cursor
        total += HOUR_VOL_MULT.get(hour, 1.0) * w
        weight += w
        cursor = seg_end
    return total / weight if weight else 1.0


def _realized_vol_per_sec(closes: list, interval_sec: int) -> float:
    """Std dev of log returns per bar, rescaled to per-second.

    Non-positive closes are filtered out BEFORE differencing — an adjacent-pair
    filter (``if closes[i] > 0 and closes[i-1] > 0``) would drop the return that
    spans a bad bar, and with two bad bars interleaved the return count falls
    below the floor and vol silently collapses to 0 — which would zero the
    spread floor and pass an unviable quote as viable.
    """
    pos = [c for c in closes if c > 0]
    if len(pos) < 3:
        return 0.0
    rets = [math.log(pos[i] / pos[i - 1]) for i in range(1, len(pos))]
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var / interval_sec) if interval_sec > 0 else 0.0


def _issue_currency_code(currency: str) -> str:
    """XRPL issue currency code: 3-char ASCII passes through; anything else must
    be the 40-char hex form.

    rippled rejects ASCII currency codes longer than 3 chars with
    ``issueMalformed`` (``RLUSD`` → error 93), which made amm_info fail and the
    fee ceiling silently fall back to the configured default. ``RLUSD`` hex is
    ``524C555344000000000000000000000000000000``.
    """
    if len(currency) == 3 and currency.isascii() and currency.isalnum():
        return currency
    if len(currency) == 40 and all(c in "0123456789ABCDEFabcdef" for c in currency):
        return currency  # already hex
    return currency.encode("ascii").hex().upper().ljust(40, "0")


def _widen_quotes(mid: float, distance_pct: float) -> tuple[float, float]:
    """Widened bid/ask at ``distance_pct`` percent each side of mid.

    Delta Raptor's spike mechanic: when the market turns hostile (floor >=
    ceiling) it does NOT stop — it keeps quoting at a wide distance from mid
    (e.g. 1%) so fills are rare but never free. mid 1.0, 1% -> (0.99, 1.01).
    """
    d = abs(distance_pct) / 100.0
    return mid * (1.0 - d), mid * (1.0 + d)


def _top_of_book_anchor(best_bid: float, best_ask: float, improve_pct: float) -> tuple[float, float]:
    """Level-1 prices: improve the current best by ``improve_pct`` percent each side.

    bid = best_bid * (1 + p/100)   (pay a hair more -> NEW best bid)
    ask = best_ask * (1 - p/100)   (ask a hair less -> NEW best ask)
    Works on any book — wide books (XRP-USDC) get quotes that still top the book.
    """
    p = abs(improve_pct) / 100.0
    return best_bid * (1.0 + p), best_ask * (1.0 - p)


def _amm_info_assets(
    xrpl_pair: str, quote_issuer: str, base_issuer: str
) -> tuple[dict, dict] | None:
    """Derive the (asset, asset2) issue pair for the pair's AMM pool.

    The pool is identified by the unordered asset pair, but amm_info still
    needs an issuer on every issued side. ``quote_issuer``/``base_issuer`` are
    the verified issuer addresses for the quote and base legs; XRP takes no
    issuer. Returns None when the pair can't be parsed or an issued leg has no
    issuer configured (caller then falls back to the configured default fee).
    """
    base, _, quote = xrpl_pair.partition("-")
    if not base or not quote:
        return None

    def issue(code: str, issuer: str) -> dict | None:
        if code == "XRP":
            return {"currency": "XRP"}
        if not issuer:
            return None
        return {"currency": _issue_currency_code(code), "issuer": issuer}

    asset = issue(quote, quote_issuer)  # quote side
    asset2 = issue(base, base_issuer)  # base side
    if asset is None or asset2 is None:
        return None
    return asset, asset2


async def _fetch_amm_fee_pct(asset: dict, asset2: dict) -> tuple[float | None, str]:
    """AMM trading fee in percent for an issue pair. Returns (fee_pct, note)."""
    payload = {
        "method": "amm_info",
        "params": [
            {
                "asset": asset,
                "asset2": asset2,
                "ledger_index": "validated",
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(XRPL_RPC, json=payload)
            data = resp.json().get("result", {})
        amm = data.get("amm")
        if not amm:
            return None, f"amm_info returned no pool ({data.get('error', 'unknown')})"
        # trading_fee is in units of 1/100_000 -> 1000 == 1% (field is lowercase
        # per the ledger schema; older docs call it TradingFee).
        raw_fee = amm.get("trading_fee", amm.get("TradingFee", 0)) or 0
        return float(raw_fee) / 1000.0, "live from amm_info"
    except Exception as exc:  # network/parse — fall back, never raise into a tick
        logger.warning("amm_info fetch failed: %s", exc)
        return None, f"amm_info unreachable ({type(exc).__name__})"


def _norm_candles(raw) -> list:
    return raw if isinstance(raw, list) else raw.get("data", raw.get("candles", []))


def _extract_ref_price(raw, pair: str, fallback: float) -> float:
    """Pull ``pair``'s price out of a prices payload, else ``fallback``.

    The endpoint returns either ``{pair: price}`` or a per-connector nesting
    (``{connector: {pair: price}}``), and a flat payload can carry non-numeric
    values (connector names) alongside the prices. Taking the first value and
    calling ``float()`` on it therefore raises or yields a bogus reference —
    and a wrong reference price silently misplaces every quote, so anything we
    can't read as a positive number falls back to the last candle close.
    """
    if not isinstance(raw, dict):
        return fallback

    def _positive(value) -> float | None:
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        return num if num > 0 else None

    direct = _positive(raw.get(pair))
    if direct is not None:
        return direct
    for value in raw.values():
        if isinstance(value, dict):
            nested = _positive(value.get(pair))
            if nested is not None:
                return nested
    return fallback


# ── routine ──────────────────────────────────────────────────────────────────


async def _pin_quote(text: str, pair: str = "") -> str:
    rows = split_kv(text)
    await pin_nest_sheet(
        title=f"Delta Raptor — perch {pair or mark_of(rows, 'xrpl_pair')}",
        slug="xrpl_mm_quote_planner",
        text=text,
        kpis=[
            ("Nest", pair or mark_of(rows, "xrpl_pair")),
            ("Live $", mark_of(rows, "live_usd")),
            ("Hold", mark_of(rows, "hold")),
        ],
        heading="PERCH / BOOK",
        blurb="Size to the observed purse. Cup $100/$800 are ceilings.",
    )
    return text


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return "No server available"

    out: list[str] = []

    # 1. Reference fair value from the CEX — never from on-ledger data.
    try:
        candles_raw, ref_prices = await asyncio.gather(
            client.market_data.get_candles(
                config.reference_connector, config.reference_pair, interval="1m", max_records=120
            ),
            client.market_data.get_prices(config.reference_connector, [config.reference_pair]),
        )
    except Exception as exc:
        return f"reference_price: ERROR fetching {config.reference_pair} — {exc}"

    candles = _norm_candles(candles_raw)
    closes = [float(c.get("close", 0)) for c in candles if float(c.get("close", 0)) > 0]
    if not closes:
        return f"reference_price: ERROR no candle data for {config.reference_pair}"

    ref_mid = _extract_ref_price(ref_prices, config.reference_pair, closes[-1])

    # RLUSD/XRP is the inverse of XRP/USD (RLUSD pegged to 1 USD).
    implied_xrpl_price = (1.0 / ref_mid) if ref_mid > 0 else 0.0

    out.append("=== REFERENCE (fair value source) ===")
    out.append(f"reference_pair: {config.reference_pair} @ {config.reference_connector}")
    out.append(f"reference_mid_usd: {ref_mid:.6f}")
    out.append(f"implied_{config.xrpl_pair}: {implied_xrpl_price:.6f}  (1 / XRP-USD)")

    observed = config.observed_wallet_usd
    buy_room = None if config.buy_room_usd < 0 else float(config.buy_room_usd)
    sell_room = float(config.inv_sellable_usd) if config.inv_sellable_usd > 0 else None
    purse_note = "purse: config ceilings (portfolio unread)"
    if observed < 0 or buy_room is None or sell_room is None:
        try:
            state = await client.portfolio.get_state()
            acct = (state or {}).get("master_account") or state or {}
            rows = acct.get("xrpl") if isinstance(acct, dict) else []
            parsed = xrpl_purse(rows or [], ref_mid, reserve_xrp=BASE_RESERVE_XRP + OWNER_RESERVE_XRP)
            if observed < 0:
                observed = parsed["wallet_usd"]
            if buy_room is None:
                buy_room = parsed["buy_room_usd"]
            if sell_room is None:
                sell_room = parsed["sell_room_usd"]
            purse_note = (
                f"purse: xrpl ${parsed['wallet_usd']:.2f}  buy_room ${parsed['buy_room_usd']:.2f}  "
                f"sell_room ${parsed['sell_room_usd']:.2f}  xrp_avail {parsed['xrp_avail']:.4f}"
            )
        except Exception as exc:
            purse_note = f"purse: portfolio unread ({exc}) — Cup ceilings only"
    if observed < 0:
        observed = config.wallet_ceiling_quote
    perch = compose_perch(
        config.hunting_mode,
        wallet=observed,
        toehold=config.toehold_quote,
        inv_sellable_usd=sell_room or 0.0,
        race_levels=config.levels_per_side,
        envelope=config.wallet_ceiling_quote,
        buy_room_usd=buy_room,
        sell_room_usd=sell_room,
    )
    live_usd = perch.live
    mode = normalize_mode(config.hunting_mode)
    out.append("")
    out.append("=== PERCH ===")
    out.append(f"hunting_mode: {perch.mode}")
    out.append(purse_note)
    out.append(
        f"wallet_usd: {perch.wallet:.2f}  live_usd: {perch.live:.2f}  idle_usd: {perch.idle:.2f}"
    )
    out.append(
        f"buy_live_usd: {perch.buy_live:.2f}  sell_live_usd: {perch.sell_live:.2f}  "
        f"one_sided: {str(perch.one_sided).lower()}  hold: {str(perch.hold).lower()}  "
        f"levels: {perch.levels_per_side}"
    )
    out.append(f"perch_note: {perch.note}")
    if perch.hold:
        out.append("suggested_action: HOLD — do not create a controller this tick")
    out.append("")

    # 2. Spread FLOOR — expected adverse move over one requote interval.
    #
    # The raw realized-vol estimate already embeds whatever hours it was measured
    # in, so multiplying it by the current hour's clock value would double-count the
    # seasonal component. Deseasonalise first (divide by the lookback's mean
    # multiplier), then re-seasonalise onto the window the quote will actually be
    # live for (multiply by the forward window's mean multiplier).
    # The floor is set by how long a quote stays exposed, which in controller mode is the
    # controller's executor_refresh_time — the bot requotes without the agent. Using the
    # LLM tick interval instead inflates the floor by sqrt(tick / refresh) and produces a
    # spurious `viable: false` that would block a deploy that is in fact viable.
    requote_sec = max(config.requote_interval_sec or config.tick_interval_sec, 1)

    vol_raw = _realized_vol_per_sec(closes, 60)
    now_ms = int(time.time() * 1000)
    look_start_ms = now_ms - len(closes) * 60_000
    fwd_end_ms = now_ms + requote_sec * 1000

    if config.use_vol_clock:
        mult_look = _hour_mult_span(look_start_ms, now_ms)
        mult_fwd = _hour_mult_span(now_ms, fwd_end_ms)
        vol_deseason = vol_raw / mult_look if mult_look > 0 else vol_raw
        vol_adj = vol_deseason * mult_fwd
    else:
        mult_look = mult_fwd = 1.0
        vol_deseason = vol_adj = vol_raw

    adverse_move = config.adverse_k * vol_adj * math.sqrt(requote_sec)
    floor_bps = adverse_move * 10_000

    def _utc(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%H:%M")

    out.append("")
    out.append("=== SPREAD FLOOR (adverse selection) ===")
    out.append(f"vol_clock_enabled: {str(config.use_vol_clock).lower()}")
    out.append(f"realized_vol_raw: {vol_raw * math.sqrt(60) * 100:.4f}% per minute")
    out.append(
        f"hour_mult_lookback: {mult_look:.3f}  "
        f"(UTC {_utc(look_start_ms)}-{_utc(now_ms)}, {len(closes)} min)"
    )
    out.append(
        f"vol_deseasonalized: {vol_deseason * math.sqrt(60) * 100:.4f}% per minute"
    )
    out.append(
        f"hour_mult_forward: {mult_fwd:.3f}  "
        f"(UTC {_utc(now_ms)}-{_utc(fwd_end_ms)}, quote live window)"
    )
    out.append(f"vol_adjusted: {vol_adj * math.sqrt(60) * 100:.4f}% per minute")
    out.append(f"tick_interval_sec: {config.tick_interval_sec}  (LLM reasoning cadence)")
    out.append(
        f"requote_interval_sec: {requote_sec}  (quote exposure — THIS sets the floor)"
    )
    if config.requote_interval_sec:
        out.append(
            "note: floor computed from requote_interval_sec, not the LLM tick — correct for "
            "controller mode, where the bot requotes on its own executor_refresh_time"
        )
    else:
        out.append(
            "note: no requote_interval_sec given, so the LLM tick is assumed to be the "
            "exposure window (executor mode). In controller mode pass executor_refresh_time "
            "or the floor will be overstated by sqrt(tick / refresh)"
        )
    out.append(f"expected_adverse_move: {floor_bps:.2f} bps over one interval")
    out.append(f"spread_floor_bps: {floor_bps:.2f}  (per side, k={config.adverse_k})")

    # 3. Spread CEILING — the AMM fee we must undercut to win pathfinding.
    #    Pool = BASE/QUOTE (asset=quote, asset2=base). XRP legs need no issuer;
    #    issued legs need their verified issuer or amm_info is skipped.
    amm_assets = _amm_info_assets(
        config.xrpl_pair, config.amm_quote_issuer, config.amm_asset2_issuer
    )
    if amm_assets:
        amm_fee_pct, fee_note = await _fetch_amm_fee_pct(*amm_assets)
    else:
        amm_fee_pct, fee_note = None, "missing issuer config — amm_info skipped"
    if amm_fee_pct is None:
        amm_fee_pct = config.amm_fee_pct_fallback
        fee_note = f"FALLBACK {amm_fee_pct}% — {fee_note}"
    ceiling_bps = amm_fee_pct * 100

    out.append("")
    out.append("=== SPREAD CEILING (AMM fee to undercut) ===")
    out.append(f"amm_trading_fee_pct: {amm_fee_pct:.4f}%   ({fee_note})")
    out.append(f"spread_ceiling_bps: {ceiling_bps:.2f}  (quote wider and flow routes to the AMM)")

    # 4. Viability — the load-bearing verdict.
    viable = floor_bps < ceiling_bps
    headroom = ceiling_bps - floor_bps
    out.append("")
    out.append("=== VIABILITY ===")
    out.append(f"viable: {str(viable).lower()}")
    out.append(f"headroom_bps: {headroom:.2f}")
    if viable:
        target = floor_bps + headroom * 0.5  # sit mid-band
        out.append(f"suggested_spread_bps_per_side: {target:.2f}")
        out.append(f"suggested_bid: {implied_xrpl_price * (1 - target / 10_000):.6f}")
        out.append(f"suggested_ask: {implied_xrpl_price * (1 + target / 10_000):.6f}")
        # pmm_simple's buy_spreads/sell_spreads are FRACTIONS of the reference price
        # (order_price = reference_price * (1 +/- spread)), not bps. Emit the converted
        # values so a bps figure can never be pasted into a controller config: 22 bps
        # entered as 22 would quote at 2200%.
        #
        # Levels are spaced strictly inside (floor, ceiling) rather than as multiples of
        # the target. A geometric ladder walks straight through the AMM fee ceiling on the
        # outer level — 3 levels at 2x/3x a 7 bps target reaches 21 bps against a 10 bps
        # ceiling, and every offer above the ceiling loses the flow to the pool.
        levels = max(perch.levels_per_side, 1)
        ladder_bps = [
            floor_bps + headroom * (i + 1) / (levels + 1) for i in range(levels)
        ]
        out.append(
            "controller_spreads (FRACTIONS for pmm_simple buy_spreads/sell_spreads — "
            "never paste bps here): "
            + ",".join(f"{b / 10_000:.6f}" for b in ladder_bps)
        )
        out.append(
            "ladder_bps: "
            + ",".join(f"{b:.2f}" for b in ladder_bps)
            + f"  (all strictly between floor {floor_bps:.2f} and ceiling {ceiling_bps:.2f})"
        )
    else:
        out.append(f"suggested_action: {'WIDEN' if config.widen_enabled else 'DO NOT QUOTE'}")
        out.append(
            "reason: adverse-selection floor meets or exceeds the AMM fee ceiling. "
            "Tight quoting would give the market a free option against us."
        )
        if config.widen_enabled:
            wid_bid, wid_ask = _widen_quotes(implied_xrpl_price, config.widen_distance_pct)
            out.append(
                f"widen_mode: KEEP QUOTING at {config.widen_distance_pct:.2f}% from mid "
                f"(do not stop — fills are rare but never free)"
            )
            if mode == "harvest":
                out.append(
                    f"harvest_spike: widen the ${live_usd:.0f} toehold only — "
                    f"idle ${perch.idle:.0f} stays off the book; do not rest ${perch.wallet:.0f}"
                )
            out.append(f"widen_bid: {wid_bid:.6f}   widen_ask: {wid_ask:.6f}")
            out.append(
                f"widen_spread_fraction (pmm_simple per-side, 1 level): "
                f"{config.widen_distance_pct / 100:.6f}"
            )
            out.append(
                "note: use a single wide level per side; the normal ladder does not "
                "apply in widen mode"
            )
        else:
            out.append(
                "Either shorten the exposure window or wait for vol to fall — in "
                "controller mode that means lowering executor_refresh_time, in "
                "executor mode frequency_sec."
            )

    # 5. Reserves and deployable capital — live perch, not the wallet ceiling.
    n_offers = perch.levels_per_side * (1 if perch.one_sided else 2)
    n_offers = max(n_offers, 1)
    reserve_xrp = BASE_RESERVE_XRP + OWNER_RESERVE_XRP * n_offers
    reserve_usd = reserve_xrp * ref_mid
    per_level = live_usd / n_offers if n_offers else 0.0

    out.append("")
    out.append("=== RESERVES & SIZING ===")
    out.append(f"levels_per_side: {perch.levels_per_side}  (offers: {n_offers})")
    out.append(f"reserve_locked_xrp: {reserve_xrp:.2f}  (~${reserve_usd:.2f})")
    out.append(f"capital_usd: ${live_usd:.2f}  (wallet ${perch.wallet:.2f}, idle ${perch.idle:.2f})")
    out.append(f"per_level_notional: ${per_level:.2f}")
    out.append("note: size against FREE balance — reserved XRP is not spendable")
    if perch.one_sided:
        out.append(
            "harvest_seed: BUY only this tick. Do not post SELL until inv_sellable_usd covers a toehold."
        )

    # A controller's total_amount_quote is denominated in the pair's QUOTE asset. On
    # RLUSD-XRP that is XRP, not USD — passing a USD figure straight through oversizes
    # the deployment by the XRP price (~3x), silently breaching the risk limit.
    quote_asset = config.xrpl_pair.split("-")[-1].upper() if "-" in config.xrpl_pair else ""
    if quote_asset == "XRP" and ref_mid > 0:
        amount_in_quote = live_usd / ref_mid
        out.append(
            f"controller_total_amount_quote: {amount_in_quote:.4f}  "
            f"({quote_asset}, = ${live_usd:.2f} / {ref_mid:.6f} XRP-USD)"
        )
        out.append(
            "WARNING: quote asset is XRP. A controller's total_amount_quote is in the "
            "QUOTE asset — pass the converted figure above, never the USD number, or the "
            "deploy is oversized by the XRP price."
        )
    else:
        out.append(
            f"controller_total_amount_quote: {live_usd:.4f}  "
            f"(quote asset '{quote_asset or 'unknown'}' — verify it is USD-denominated "
            "before passing this to a controller)"
        )

    # 6. Live XRPL book state. Absence is a hard stop, not a warning.
    out.append("")
    out.append("=== XRPL BOOK ===")
    try:
        # Signature is get_order_book(connector_name, trading_pair, depth=10) —
        # passing the pair positionally binds it to connector_name and collides
        # with the keyword ("got multiple values for argument 'connector_name'").
        book = await client.market_data.get_order_book("xrpl", config.xrpl_pair)
        bids = (book or {}).get("bids") or []
        asks = (book or {}).get("asks") or []
        if bids and asks:
            best_bid = float(bids[0][0] if isinstance(bids[0], (list, tuple)) else bids[0]["price"])
            best_ask = float(asks[0][0] if isinstance(asks[0], (list, tuple)) else asks[0]["price"])
            mid = (best_bid + best_ask) / 2
            out.append(f"best_bid: {best_bid:.6f}")
            out.append(f"best_ask: {best_ask:.6f}")
            out.append(f"book_spread_bps: {(best_ask - best_bid) / mid * 10_000:.2f}")
            if implied_xrpl_price > 0:
                div = (mid / implied_xrpl_price - 1) * 10_000
                out.append(f"divergence_vs_reference_bps: {div:+.2f}")
                out.append(
                    "note: large divergence means STALE DATA, not opportunity — verify before acting"
                )
            # Top-of-book quotes: improve the current best by top_of_book_improve_pct
            # so our orders are the NEW BEST on both sides — wide books (XRP-USDC)
            # still get topped. Only meaningful when viable (widen mode ignores it).
            if viable and config.quote_mode == "top_of_book" and best_bid < best_ask:
                bid1, ask1 = _top_of_book_anchor(best_bid, best_ask, config.top_of_book_improve_pct)
                levels = max(perch.levels_per_side, 1)
                step_bps = headroom / (levels + 1)  # ladder spacing from the viability math
                bid_levels = [bid1 * (1 - step_bps * i / 10_000) for i in range(levels)]
                ask_levels = [ask1 * (1 + step_bps * i / 10_000) for i in range(levels)]
                out.append("")
                out.append("=== TOP-OF-BOOK QUOTES ===")
                out.append(
                    f"quote_mode: top_of_book — improve current best by "
                    f"{config.top_of_book_improve_pct:.3f}% per side (NEW BEST orders)"
                )
                out.append(f"level1_bid: {bid1:.6f}   level1_ask: {ask1:.6f}")
                out.append("bid_levels: " + ",".join(f"{b:.6f}" for b in bid_levels))
                out.append("ask_levels: " + ",".join(f"{a:.6f}" for a in ask_levels))
                # pmm_simple buy/sell_spreads are FRACTIONS of the controller's
                # reference price. Anchor the controller reference at the BOOK MID
                # and express each level as its distance from it.
                buy_fracs = [(mid - b) / mid for b in bid_levels]
                sell_fracs = [(a - mid) / mid for a in ask_levels]
                out.append(
                    "controller_top_of_book_spreads (fractions — set controller reference "
                    "price = BOOK MID): "
                    + "buy: " + ",".join(f"{f:.6f}" for f in buy_fracs)
                    + "  sell: " + ",".join(f"{f:.6f}" for f in sell_fracs)
                )
        else:
            out.append("status: EMPTY OR UNAVAILABLE — do not quote blind")
    except Exception as exc:
        out.append(f"status: ERROR — {exc}")
        out.append("action: treat as a hard stop; do not place offers without live book state")

    return await _pin_quote("\n".join(out), config.xrpl_pair)
