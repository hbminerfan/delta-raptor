"""Nest change: which book to sit, and how to swap leftovers to get there.

Not a Condor routine. Hunt scorer imports it.

A hop is curious, not greedy. Core nest is RLUSD-XRP. A rotation nest only
gets the live perch when *that pair's* hourly volume is up by
``curious_surge_pct`` or more. Absolute tape on RLUSD does not block the hop.
If the surge fades next hour, the bird flies home.

Inventory is token sets, not pair strings. RLUSD-XRP → XRP-USDC means pay
RLUSD, receive USDC. The whitelist book that holds both is USDC-RLUSD
(connector BASE-QUOTE). BUY there = pay RLUSD, get USDC.
"""
from __future__ import annotations

from dataclasses import dataclass

CORE_NEST = "RLUSD-XRP"

# Connector names from the strategy table. Order does not matter for matching.
DEFAULT_BOOKS = (
    "RLUSD-XRP",
    "USDC-RLUSD",
    "BBRL-RLUSD",
    "EUROP-XRP",
    "XRP-USDC",
    "EUROP-RLUSD",
)


def nest_legs(pair: str) -> tuple[str, str]:
    base, sep, quote = pair.partition("-")
    if not sep or not base or not quote:
        raise ValueError(f"bad pair {pair!r}")
    return base.upper(), quote.upper()


def nest_tokens(pair: str) -> frozenset[str]:
    b, q = nest_legs(pair)
    return frozenset((b, q))


@dataclass(frozen=True)
class Hop:
    from_pair: str
    to_pair: str
    bridge_pair: str
    side: str  # BUY = pay quote receive base on bridge; SELL = pay base receive quote
    pay_asset: str
    receive_asset: str
    note: str

    @property
    def needed(self) -> bool:
        return True


def plan_hop(
    from_pair: str,
    to_pair: str,
    books: tuple[str, ...] | list[str] = DEFAULT_BOOKS,
) -> Hop | None:
    """How to morph inventory from one nest to another. None = already there or no path."""
    if nest_tokens(from_pair) == nest_tokens(to_pair):
        return None
    extra = nest_tokens(from_pair) - nest_tokens(to_pair)
    missing = nest_tokens(to_pair) - nest_tokens(from_pair)
    if len(extra) != 1 or len(missing) != 1:
        return None
    pay = next(iter(extra))
    recv = next(iter(missing))
    for book in books:
        if nest_tokens(book) != frozenset((pay, recv)):
            continue
        base, quote = nest_legs(book)
        if recv == base and pay == quote:
            side = "BUY"
        elif recv == quote and pay == base:
            side = "SELL"
        else:
            continue
        return Hop(
            from_pair=from_pair,
            to_pair=to_pair,
            bridge_pair=book,
            side=side,
            pay_asset=pay,
            receive_asset=recv,
            note=(
                f"{side} {recv} on {book} (pay {pay}) so the nest can sit {to_pair}"
            ),
        )
    return None


def pick_nest(
    scored: list[dict],
    core_pair: str = CORE_NEST,
    curious_surge_pct: float = 50.0,
    parked_pair: str | None = None,
    books: tuple[str, ...] | list[str] = DEFAULT_BOOKS,
) -> str:
    """Park on the loudest rotation nest whose own hourly volume is up enough.

    Core stays home unless a *non-core* huntable pair printed volume_change_pct
    ≥ ``curious_surge_pct``. RLUSD being the biggest book does not veto that.
    Skip a nest if inventory cannot hop there from ``parked_pair``.
    """
    huntable = [r for r in scored if r.get("score", 0) > 0]
    if not huntable:
        return core_pair
    here = parked_pair or core_pair
    curios = [
        r
        for r in huntable
        if r["pair"] != core_pair
        and float(r.get("volume_change_pct") or 0) >= curious_surge_pct
    ]
    curios.sort(key=lambda r: float(r.get("volume_change_pct") or 0), reverse=True)
    for r in curios:
        dest = r["pair"]
        if nest_tokens(here) == nest_tokens(dest):
            return dest
        if plan_hop(here, dest, books) is not None:
            return dest
    names = {r["pair"] for r in huntable}
    return core_pair if core_pair in names else huntable[0]["pair"]
