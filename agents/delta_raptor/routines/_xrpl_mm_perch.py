"""How much of the wallet actually sits on the book.

This is not a Condor routine (no ``run`` / Config). Quote planner and hunt
scorer import it.

Two hunting modes — same bird, different appetite:

- ``race``    Mode 1. Someone is paying for volume. Whole envelope on the
              loud pair. Widen ~1% and stay. Missed fill costs the race.
- ``harvest`` Mode 2. No rebate. A toehold quotes; the rest of the wallet
              stays idle. Hunt still flies the toehold (not the idle pile).
              A spike widens the toehold — it does not rest the envelope.

Cup numbers (``envelope`` $800, ``toehold`` $100) are **ceilings**. Organizer
and smoke wallets are smaller: live size shrinks to the purse. A $85 book
does not pretend it is a $800 race.

Do not mix the modes in one deploy.
"""
from __future__ import annotations

from dataclasses import dataclass

RACE = "race"
HARVEST = "harvest"

_RACE_ALIASES = {
    "race",
    "mode1",
    "mode_1",
    "incentivized",
    "full",
    "envelope",
}

# Harvest never parks more than this slice of the *observed* purse on tick 0.
# Cup $800 → $100 toehold still wins (min(100, 160)). $85 → ~$17.
HARVEST_FRAC = 0.20
MIN_LIVE_USD = 5.0


def normalize_mode(raw: str | None) -> str:
    token = str(raw or HARVEST).strip().lower().replace("-", "_")
    return RACE if token in _RACE_ALIASES else HARVEST


@dataclass(frozen=True)
class Perch:
    mode: str
    wallet: float
    live: float
    idle: float
    buy_live: float
    sell_live: float
    levels_per_side: int
    one_sided: bool
    note: str
    hold: bool = False


def _pos(x: float | None) -> float:
    return max(float(x or 0.0), 0.0)


def xrpl_purse(
    rows: list[dict],
    xrp_usd: float,
    base: str = "RLUSD",
    quote: str = "XRP",
    reserve_xrp: float = 1.2,
) -> dict:
    """Fold an XRPL portfolio list into buy-room / sell-room USD.

    ``available_units`` is what can be offered. Negative available XRP means
    leftover offers own the reserve — buy room is zero until those cancel.
    """
    xrp_usd = _pos(xrp_usd)
    by_tok: dict[str, dict] = {}
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        tok = str(r.get("token") or r.get("asset") or r.get("currency") or "").upper()
        if "." in tok:
            tok = tok.split(".")[-1]
        if not tok:
            continue
        units = float(r.get("units") or r.get("amount") or r.get("balance") or 0)
        avail = r.get("available_units")
        if avail is None:
            avail = r.get("available")
        if avail is None:
            avail = r.get("free")
        avail_f = float(avail) if avail is not None else units
        val = float(r.get("value") or 0)
        by_tok[tok] = {"units": units, "avail": avail_f, "value": val}

    quote_row = by_tok.get(quote.upper(), {})
    base_row = by_tok.get(base.upper(), {})
    xrp_row = by_tok.get("XRP", {})

    wallet = _pos(sum(float(r.get("value") or 0) for r in (rows or []) if isinstance(r, dict)))
    if wallet <= 0:
        wallet = _pos(base_row.get("value")) + _pos(quote_row.get("value"))

    # BUY on BASE-QUOTE pays quote. On RLUSD-XRP that is XRP after owner reserve.
    if quote.upper() == "XRP":
        free_xrp = max(float(xrp_row.get("avail") or 0), 0.0)
        tradable_xrp = max(free_xrp - _pos(reserve_xrp), 0.0)
        buy_room = tradable_xrp * xrp_usd
    else:
        buy_room = max(float(quote_row.get("avail") or 0), 0.0)
        px = float(quote_row.get("value") or 0) / max(float(quote_row.get("units") or 0), 1e-12)
        if px > 0:
            buy_room *= px

    sell_room = max(float(base_row.get("avail") or 0), 0.0)
    if base.upper() != "XRP":
        # issued stables ≈ $1; prefer marked value when units look like tokens
        marked = _pos(base_row.get("value"))
        if marked > 0:
            sell_room = min(sell_room, marked) if sell_room > 0 else marked
            if float(base_row.get("avail") or 0) > 0 and marked > 0:
                sell_room = marked * (
                    max(float(base_row.get("avail") or 0), 0.0)
                    / max(float(base_row.get("units") or 0), 1e-12)
                )

    return {
        "wallet_usd": wallet,
        "buy_room_usd": _pos(buy_room),
        "sell_room_usd": _pos(sell_room),
        "xrp_avail": float(xrp_row.get("avail") or 0),
        "base_avail": float(base_row.get("avail") or 0),
    }


def compose_perch(
    mode: str | None,
    wallet: float = 800.0,
    toehold: float = 100.0,
    inv_sellable_usd: float = 0.0,
    race_levels: int = 3,
    envelope: float | None = None,
    buy_room_usd: float | None = None,
    sell_room_usd: float | None = None,
    harvest_frac: float = HARVEST_FRAC,
    min_live: float = MIN_LIVE_USD,
) -> Perch:
    """Pick live vs idle cash for this tick.

    ``wallet`` is the *observed* purse when known, else the config ceiling.
    ``envelope`` / ``toehold`` are Cup ceilings, not a promise the book is that
    big. ``buy_room_usd`` / ``sell_room_usd`` are free-balance rooms after
    reserve. ``None`` means unknown (do not constrain that side).
    """
    mode = normalize_mode(mode)
    observed = _pos(wallet)
    ceiling = _pos(envelope if envelope is not None else observed)
    purse = min(observed, ceiling) if ceiling else observed
    toehold = _pos(toehold)
    inv = _pos(inv_sellable_usd)
    levels = max(int(race_levels or 1), 1)
    frac = min(max(float(harvest_frac), 0.0), 1.0)
    sell_cap = None if sell_room_usd is None else _pos(sell_room_usd)
    if sell_cap is None:
        sell_cap = inv  # harvest: inventory unlocks SELL; race ignores 0 via branch below

    if mode == RACE:
        live = purse
        half = live / 2.0 if live else 0.0
        buy = half if buy_room_usd is None else min(half, _pos(buy_room_usd))
        sell = half if sell_room_usd is None else min(half, _pos(sell_room_usd))
        sides = max(buy, sell)
        hold = sides + 1e-12 < min_live
        return Perch(
            mode=RACE,
            wallet=purse,
            live=0.0 if hold else live,
            idle=purse if hold else 0.0,
            buy_live=0.0 if hold else buy,
            sell_live=0.0 if hold else sell,
            levels_per_side=1 if (buy < 1e-9 or sell < 1e-9) else levels,
            one_sided=min(buy, sell) < 1e-9,
            hold=hold,
            note=(
                "HOLD: purse too small to race"
                if hold
                else "race: envelope on the book — missed fill costs the race"
            ),
        )

    slice_cap = purse * frac if frac > 0 else purse
    live = min(toehold, purse, slice_cap if slice_cap > 0 else toehold)
    buy_want = live
    sell_want = min(live, sell_cap)

    if buy_room_usd is not None:
        buy_want = min(buy_want, _pos(buy_room_usd))
    # No free quote (XRP locked): sit SELL of inventory instead of a doomed BUY.
    if buy_want < 1e-9 and sell_cap >= min_live:
        sell_want = min(live, sell_cap)
        buy_want = 0.0
        live = sell_want
        seed = "SELL inventory (no free XRP for a BUY)"
    elif sell_want < 1e-9:
        live = buy_want
        seed = "BUY only until a fill unlocks SELL"
    else:
        live = max(buy_want, sell_want)
        seed = "SELL recycled from inventory"

    idle = max(purse - live, 0.0)
    hold = live + 1e-12 < min_live
    if hold:
        return Perch(
            mode=HARVEST,
            wallet=purse,
            live=0.0,
            idle=purse,
            buy_live=0.0,
            sell_live=0.0,
            levels_per_side=1,
            one_sided=True,
            hold=True,
            note="HOLD: purse/rooms below min live — do not quote",
        )
    return Perch(
        mode=HARVEST,
        wallet=purse,
        live=live,
        idle=idle,
        buy_live=buy_want,
        sell_live=sell_want,
        levels_per_side=1,
        one_sided=min(buy_want, sell_want) < 1e-9,
        hold=False,
        note=f"harvest: live ${live:.0f} of ${purse:.0f} purse, idle off; {seed}",
    )
