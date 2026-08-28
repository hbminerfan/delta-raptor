"""Plan XRPL AMM rebalancing: trigger detection, venue choice, cost gating.

Deterministic rebalance planner for the XRPL maker. Decides WHEN to rebalance
inventory (trigger: base share outside target +/- band) and HOW (venue: the
cheaper of CLOB crossing vs the AMM pool, priced in bps), and refuses to
rebalance when the cheaper venue exceeds ``max_rebalance_cost_bps``.

Venue cost model (all in bps of the traded notional):
- CLOB: full book cross = ``book_spread_bps`` (aggressive taker both sides).
- AMM:  pool trading fee (live ``amm_info``) + price impact from a constant-
        product swap with fee:  out = R_out * in' / (R_in + in'),  in' = in*(1-fee),
        impact = (mid_out - out) / mid_out.

The agent executes the verdict: REBALANCE_AMM via ``manage_amm(action=
"execute_swap", ...)`` on the pool address; REBALANCE_CLOB via a LIMIT order
that crosses the book; HOLD means wait (widen quotes) — never rebalance at
any cost.
"""
import logging
import time

import httpx
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from config_manager import get_client
from agents.delta_raptor.routines.xrpl_mm_quote_planner import (
    XRPL_RPC,
    _amm_info_assets,
)
from agents.delta_raptor.routines._xrpl_mm_sheet import mark_of, pin_nest_sheet, split_kv

logger = logging.getLogger(__name__)

CATEGORY = "Analysis"


class Config(BaseModel):
    """Plan an inventory rebalance for an XRPL pair."""

    xrpl_pair: str = Field(default="RLUSD-XRP", description="Pair as the xrpl connector names it (BASE-QUOTE)")
    base_issuer: str = Field(default="", description="Issuer of the BASE asset (blank = XRP)")
    quote_issuer: str = Field(default="", description="Issuer of the QUOTE asset when not XRP")
    base_balance: float = Field(default=0.0, description="Current base asset balance")
    quote_balance: float = Field(default=0.0, description="Current quote asset balance")
    base_price_quote: float = Field(default=0.0, description="Price of base in quote units (CEX reference)")
    inventory_target_pct: float = Field(default=50.0, description="Target base share of portfolio, %")
    inventory_band_pct: float = Field(default=1.0, description="Dead band around target, % (trigger)")
    max_rebalance_cost_bps: float = Field(default=25.0, description="Refuse rebalance if cheaper venue costs more, bps")
    max_rebalance_amount_quote: float = Field(default=50.0, description="Per-tick cap on rebalance notional, quote units")
    progressive_frac: float = Field(default=0.5, description="Rebalance this fraction of the excess (progressive, not full)")
    amm_fee_pct_fallback: float = Field(default=0.1, description="AMM fee % if amm_info unreachable")
    book_spread_bps: float | None = Field(default=None, description="CLOB full-cross spread in bps (None = unknown)")


# ── pure math (unit-tested) ──────────────────────────────────────────────────


def _inventory_state(base_balance: float, quote_balance: float, price: float) -> dict:
    """Base share of portfolio value. Returns (base_value, quote_value, share)."""
    base_value = base_balance * price if price > 0 else 0.0
    total = base_value + quote_balance
    share = base_value / total * 100.0 if total > 0 else 0.0
    return {"base_value": base_value, "quote_value": quote_balance, "base_share": share}


def _rebalance_signal(share: float, target: float, band: float) -> dict:
    """Trigger: base share outside target +/- band. Returns side + deviation."""
    dev = share - target
    if abs(dev) <= band:
        return {"needed": False, "side": "NONE", "deviation": dev}
    return {"needed": True, "side": "SELL_BASE" if dev > 0 else "BUY_BASE", "deviation": dev}


def _progressive_amount(excess_value: float, frac: float, max_amount: float) -> float:
    """Half-step (or configured fraction) of the excess, capped per tick."""
    if excess_value <= 0:
        return 0.0
    return min(excess_value * frac, max_amount)


def _amm_swap_cost_bps(fee_frac: float, amount_in: float, reserve_in: float, reserve_out: float) -> float:
    """AMM cost = fee + price impact (constant product with fee), in bps.

    ``fee_frac`` is the pool fee as a FRACTION (0.00208 = 0.208%).
    """
    if amount_in <= 0 or reserve_in <= 0 or reserve_out <= 0:
        return float("inf")
    fee_bps = fee_frac * 10_000.0
    in_after_fee = amount_in * (1.0 - fee_frac)
    out = reserve_out * in_after_fee / (reserve_in + in_after_fee)
    # Baseline uses the SAME fee-adjusted input, so the gap vs `out` is pure
    # price impact — the fee is already in fee_bps. Using the gross input here
    # double-counts the fee as impact.
    mid_out = in_after_fee * reserve_out / reserve_in
    impact_bps = (mid_out - out) / mid_out * 10_000.0 if mid_out > 0 else 0.0
    return fee_bps + max(impact_bps, 0.0)


def _clob_cross_cost_bps(book_spread_bps: float | None) -> float | None:
    """CLOB rebalance cost: crossing the full book spread. Unknown book -> None."""
    return book_spread_bps if book_spread_bps is not None else None


def _choose_venue(clob_bps: float | None, amm_bps: float | None, max_bps: float) -> tuple[str, float | None]:
    """Pick the cheaper venue, gated by max cost. Returns (venue, cost_bps)."""
    available = [c for c in (("CLOB", clob_bps), ("AMM", amm_bps)) if c[1] is not None]
    if not available:
        return "HOLD", None
    venue, cost = min(available, key=lambda c: c[1])
    if cost > max_bps:
        return "HOLD", cost
    return venue, cost


def _amount_from_amm_value(v) -> float:
    """amm_info amount field: XRP = drops string, issued = {value: str}."""
    if isinstance(v, dict):
        return float(v.get("value", 0) or 0)
    try:
        return float(v or 0) / 1_000_000.0  # drops -> XRP
    except (TypeError, ValueError):
        return 0.0


# ── routine ──────────────────────────────────────────────────────────────────


async def _fetch_amm_pool(asset: dict, asset2: dict) -> tuple[float | None, dict | None, str]:
    """Live pool fee %, reserves {in, out} undecided, AMM account. Returns (fee_pct, pool, note)."""
    payload = {
        "method": "amm_info",
        "params": [{"asset": asset, "asset2": asset2, "ledger_index": "validated"}],
    }
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.post(XRPL_RPC, json=payload)
            data = resp.json().get("result", {})
        pool = data.get("amm")
        if not pool:
            return None, None, f"amm_info returned no pool ({data.get('error', 'unknown')})"
        raw = pool.get("trading_fee", pool.get("TradingFee", 0)) or 0
        return float(raw) / 1000.0, pool, "live from amm_info"
    except Exception as exc:  # network/parse — fall back, never raise into a tick
        logger.warning("amm_info fetch failed: %s", exc)
        return None, None, f"amm_info unreachable ({type(exc).__name__})"


async def _pin_rebalance(text: str, pair: str = "") -> str:
    rows = split_kv(text)
    await pin_nest_sheet(
        title=f"Delta Raptor — rebalance {pair or mark_of(rows, 'xrpl_pair')}",
        slug="xrpl_mm_rebalance_planner",
        text=text,
        kpis=[
            ("Action", mark_of(rows, "action")),
            ("Trigger", mark_of(rows, "trigger")),
        ],
        heading="INVENTORY / VENUE",
        blurb="Cheap-only. Never rebalance at any cost.",
    )
    return text


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return "No server available"

    out: list[str] = []

    base, _, quote = config.xrpl_pair.partition("-")
    if not base or not quote:
        return f"rebalance: ERROR malformed pair {config.xrpl_pair!r}"

    # 1. Inventory state + trigger
    state = _inventory_state(config.base_balance, config.quote_balance, config.base_price_quote)
    sig = _rebalance_signal(state["base_share"], config.inventory_target_pct, config.inventory_band_pct)
    total_value = state["base_value"] + state["quote_value"]

    trigger_msg = (
        "NO — within band, HOLD"
        if not sig["needed"]
        else f"YES — {sig['side']} (deviation {sig['deviation']:+.2f}pp)"
    )
    out.append("=== INVENTORY ===")
    out.append(f"base_balance: {config.base_balance:.4f} {base}  (~${state['base_value']:.2f})")
    out.append(f"quote_balance: {config.quote_balance:.4f} {quote}  (${state['quote_value']:.2f})")
    out.append(f"base_share: {state['base_share']:.2f}%  (target {config.inventory_target_pct:.0f}% ± {config.inventory_band_pct:.0f}%)")
    out.append(f"trigger: {trigger_msg}")

    if not sig["needed"] or total_value <= 0:
        out.append("")
        out.append("=== VERDICT ===")
        out.append("action: HOLD")
        out.append("reason: no rebalance needed")
        return await _pin_rebalance("\n".join(out), config.xrpl_pair)

    # 2. Progressive amount
    excess_value = abs(sig["deviation"]) / 100.0 * total_value
    amount_quote = _progressive_amount(excess_value, config.progressive_frac, config.max_rebalance_amount_quote)
    amount_base = amount_quote / config.base_price_quote if config.base_price_quote > 0 else 0.0
    out.append("")
    out.append("=== REBALANCE SIZE (progressive) ===")
    out.append(f"excess_value: ${excess_value:.2f} ({abs(sig['deviation']):.2f}pp of ${total_value:.2f})")
    out.append(f"progressive_frac: {config.progressive_frac:.0%}  cap: ${config.max_rebalance_amount_quote:.2f}")
    out.append(f"amount_quote: {amount_quote:.4f} {quote}  amount_base: {amount_base:.4f} {base}")
    if amount_quote <= 0:
        out.append("")
        out.append("=== VERDICT ===")
        out.append("action: HOLD")
        out.append("reason: rebalance amount rounds to zero")
        return await _pin_rebalance("\n".join(out), config.xrpl_pair)

    # 3. Venue costs
    clob_bps = _clob_cross_cost_bps(config.book_spread_bps)
    out.append("")
    out.append("=== VENUE COSTS (bps of notional) ===")
    out.append(f"clob_book_spread_bps: {clob_bps:.2f}" if clob_bps is not None else "clob_book_spread_bps: UNKNOWN (no book)")

    amm_bps = None
    amm_note = ""
    assets = _amm_info_assets(config.xrpl_pair, config.quote_issuer, config.base_issuer)
    if assets:
        fee_pct, pool, amm_note = await _fetch_amm_pool(*assets)
        if pool:
            # amm_info: amount = quote-leg reserve, amount2 = base-leg reserve
            quote_res = _amount_from_amm_value(pool.get("amount"))
            base_res = _amount_from_amm_value(pool.get("amount2"))
            if sig["side"] == "SELL_BASE":
                reserve_in, reserve_out = base_res, quote_res
                input_amount = amount_base
            else:
                reserve_in, reserve_out = quote_res, base_res
                input_amount = amount_quote
            amm_bps = _amm_swap_cost_bps(
                (fee_pct or config.amm_fee_pct_fallback) / 100.0,  # percent -> fraction
                input_amount, reserve_in, reserve_out,
            )
            out.append(f"amm_pool: {pool.get('account', '?')}  fee: {(fee_pct or config.amm_fee_pct_fallback):.4f}%  reserves: {base_res:.0f} {base} / {quote_res:.0f} {quote}")
            out.append(f"amm_cost_bps (fee + impact): {amm_bps:.2f}")
        else:
            out.append(f"amm_cost_bps: UNAVAILABLE ({amm_note})")
    else:
        out.append(f"amm_cost_bps: UNAVAILABLE (missing issuer config)")

    venue, cost = _choose_venue(clob_bps, amm_bps, config.max_rebalance_cost_bps)
    out.append("")
    out.append("=== VERDICT ===")
    if venue == "HOLD":
        out.append("action: HOLD")
        out.append(
            f"reason: cheapest venue ("
            + ("AMM" if amm_bps is not None and (clob_bps is None or amm_bps <= clob_bps) else "CLOB")
            + f") costs {cost:.2f} bps > max_rebalance_cost_bps {config.max_rebalance_cost_bps:.2f}. "
            "Widen quotes and wait — do not rebalance at any cost."
        )
        return await _pin_rebalance("\n".join(out), config.xrpl_pair)

    out.append(f"action: {'REBALANCE_AMM' if venue == 'AMM' else 'REBALANCE_CLOB'}")
    out.append(f"side: {sig['side']}")
    out.append(f"amount_base: {amount_base:.6f}  amount_quote: {amount_quote:.4f}")
    out.append(f"venue_cost_bps: {cost:.2f}  (clob={clob_bps if clob_bps is not None else 'n/a'}, amm={amm_bps if amm_bps is not None else 'n/a'})")
    if venue == "AMM" and assets:
        # re-fetch pool to get account (cheap; we already know it exists)
        out.append(f"execute: manage_amm(action=\"execute_swap\", connector=\"xrpl\", network=\"mainnet\", "
                   f"pool_address=\"{pool.get('account')}\", base_token=\"{base}\", "
                   f"side=\"{'SELL' if sig['side'] == 'SELL_BASE' else 'BUY'}\", "
                   f"amount=\"{amount_base:.6f}\", slippage_pct=\"0.5\")")
    elif venue == "CLOB":
        out.append(f"execute: LIMIT order crossing the book (taker) — side {sig['side']}, amount {amount_base:.6f} {base}")
    return await _pin_rebalance("\n".join(out), config.xrpl_pair)
