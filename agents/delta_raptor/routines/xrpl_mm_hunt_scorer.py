"""XRPL maker hunt scorer: rank candidate pairs by maker opportunity.

Replaces the v1 "volume-delta hunt" (XRPLiquid leaderboard epoch volume — the
rewards program is stopped, so leaderboard deltas no longer map to anything we
earn). The hunt now scores OUR OWN maker opportunity per pair, from the quote
planner's outputs and self-reported fill stats — no external leaderboard, no
competitor wallet address.

Score (pure heuristic, monotonic in the right directions):

    score = headroom_bps * fill_rate_norm / max(book_spread_bps, 1.0)

- headroom_bps   — planner's ceiling - floor (spread capture potential)
- fill_rate_norm — own fills per second, normalized to a reference rate,
                   capped at 3x (pairs with no history get a small prior so
                   they are scored, not excluded)
- book_spread_bps — wider books score worse (harder to fill, more adverse
                    selection)

Escape-gate design: core capital stays on the deepest pair (RLUSD-XRP). A slice
rotates to another pair ONLY when its score beats the core's by
``hunt_beat_threshold`` AND it has a live book — thin/junk books score 0.

The agent feeds planner outputs + executor fill counts into this routine via
config; the routine is deterministic and makes no API calls.
"""
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

from agents.delta_raptor.routines._xrpl_mm_perch import HARVEST, compose_perch
from agents.delta_raptor.routines._xrpl_mm_hop import DEFAULT_BOOKS, nest_tokens, pick_nest, plan_hop
from agents.delta_raptor.routines._xrpl_mm_sheet import mark_of, pin_nest_sheet, split_kv

CATEGORY = "Analysis"


class PairSignal(BaseModel):
    """Planner + execution facts for one candidate pair this window."""

    pair: str
    viable: bool = Field(default=False, description="Quote planner verdict")
    book_ok: bool = Field(default=False, description="Live non-empty XRPL book")
    headroom_bps: float = Field(default=0.0, description="Planner ceiling - floor, bps")
    book_spread_bps: float | None = Field(default=None, description="Book spread, bps")
    fills_window: int = Field(default=0, description="Own fills in the window")
    window_sec: int = Field(default=1800, description="Fill-count window, seconds")
    volume_change_pct: float = Field(
        default=0.0,
        description="Market volume change over the last hour, % (e.g. 150 = +150% surge). "
        "Agent supplies from GeckoTerminal pool volume or own observed fills; 0 if unknown.",
    )


class Config(BaseModel):
    """Score candidate pairs and allocate capital by hourly volume change."""

    pairs: list[PairSignal]
    core_pair: str = Field(default="RLUSD-XRP", description="Pair that keeps the core capital")
    hunt_beat_threshold: float = Field(default=1.2, description="Rotation pair must score this x the core (legacy)")
    hunt_slice_pct: float = Field(default=0.15, description="Capital slice to rotate, fraction of total (legacy)")
    fill_rate_ref: float = Field(default=1 / 60, description="Reference fills/sec (1 fill per minute)")
    fill_prior: float = Field(default=0.5, description="Prior fills per window for pairs with no history")
    max_rotation_spread_bps: float = Field(default=30.0, description="Reject rotation pairs with wider books")
    volume_weight: float = Field(
        default=1.0,
        description="How strongly a 100% hourly volume surge boosts the score (x1 multiplier at weight 1.0)",
    )
    total_capital_quote: float = Field(
        default=800.0, description="Total capital to allocate across huntable pairs (competition: $800)"
    )
    min_share_pct: float = Field(
        default=0.0,
        description="Unused in hunt mode (winner-take-all). Kept for config compatibility.",
    )
    full_shift_dominance: float = Field(
        default=3.0,
        description="If the top pair's volume weight >= this x the second's, it gets 100% of capital",
    )
    base_weight: float = Field(
        default=50.0, description="Baseline volume weight so a pair with 0 volume change keeps a share"
    )
    hunting_mode: str = Field(
        default="harvest",
        description="race = park the envelope; harvest = park the toehold, idle stays off",
    )
    toehold_quote: float = Field(
        default=100.0, description="Harvest live USD that a nest hop may move"
    )
    curious_surge_pct: float = Field(
        default=50.0,
        description="Leave core when a rotation pair's own hourly volume is up this % or more",
    )
    parked_pair: str = Field(
        default="RLUSD-XRP", description="Nest currently quoting — used to emit CONVERT"
    )


# ── pure math (unit-tested) ──────────────────────────────────────────────────


def _fill_rate(fills: int, window_sec: int, prior: float) -> float:
    return (fills + prior) / max(window_sec, 1)


def _volume_boost(volume_change_pct: float, weight: float) -> float:
    """Score multiplier from hourly volume surge. Only boosts, never penalizes.

    A pair that just became active (+200% volume) scores 3x higher at weight 1.0;
    a pair with falling volume gets no bonus and no penalty.
    """
    surge = max(volume_change_pct, 0.0) / 100.0
    return 1.0 + surge * weight


def pair_score(s: PairSignal, ref_fill_rate: float, fill_prior: float, volume_weight: float = 1.0) -> float:
    """Opportunity score for one pair. 0 = not huntable."""
    if not s.viable or not s.book_ok:
        return 0.0
    spread = s.book_spread_bps
    if spread is None or spread <= 0:
        return 0.0
    fr = _fill_rate(s.fills_window, s.window_sec, fill_prior)
    fill_norm = min(fr / ref_fill_rate, 3.0)  # cap so one hot pair can't dominate
    return s.headroom_bps * fill_norm / max(spread, 1.0) * _volume_boost(s.volume_change_pct, volume_weight)


def rank_pairs(
    signals: list[PairSignal], ref_fill_rate: float = 1 / 60, fill_prior: float = 0.5, volume_weight: float = 1.0
) -> list[dict]:
    """Rank pairs by opportunity score, descending. Returns score dicts."""
    scored = []
    for s in signals:
        score = pair_score(s, ref_fill_rate, fill_prior, volume_weight)
        scored.append(
            {
                "pair": s.pair,
                "score": score,
                "headroom_bps": s.headroom_bps if (s.viable and s.book_ok) else 0.0,
                "fill_rate": _fill_rate(s.fills_window, s.window_sec, fill_prior),
                "volume_change_pct": s.volume_change_pct,
                "viable": s.viable,
                "book_ok": s.book_ok,
            }
        )
    return sorted(scored, key=lambda r: r["score"], reverse=True)


def decide_rotation(
    ranked: list[dict],
    core_pair: str,
    beat_threshold: float = 1.2,
    max_spread_bps: float = 30.0,
) -> tuple[str, str | None, float]:
    """Escape-gate: rotate a slice only if a non-core pair clearly beats the core.

    Returns (action, target_pair, target_score). (Legacy — capital allocation
    supersedes slice rotation; kept for backward compatibility.)
    """
    core = next((r for r in ranked if r["pair"] == core_pair), None)
    if not core or core["score"] <= 0:
        return "HOLD", None, 0.0
    for r in ranked:
        if r["pair"] == core_pair:
            continue
        if r["score"] > core["score"] * beat_threshold:
            return "ROTATE_SLICE", r["pair"], r["score"]
    return "HOLD", None, 0.0


def _volume_weight(volume_change_pct: float, base: float, cap: float = 400.0) -> float:
    """Allocation weight: baseline + capped positive volume surge."""
    return base + min(max(volume_change_pct, 0.0), cap)


def allocate_capital(
    scored: list[dict],
    total_quote: float,
    min_share_pct: float = 0.0,
    dominance: float = 3.0,
    base_weight: float = 50.0,
    core_pair: str = "RLUSD-XRP",
    curious_surge_pct: float = 50.0,
    parked_pair: str | None = None,
) -> list[dict]:
    """Park the live perch on one nest.

    Not 3× vs RLUSD. A rotation nest wins when *its* hourly volume_change_pct
    ≥ ``curious_surge_pct``. Otherwise home to ``core_pair``. Winner-take-all.
    ``dominance`` is unused (kept so old call sites still type-check).
    """
    del min_share_pct, dominance, base_weight
    huntable = [r for r in scored if r["score"] > 0]
    if not huntable:
        return [{**r, "share_pct": 0.0, "capital_quote": 0.0} for r in scored]

    park = pick_nest(
        scored,
        core_pair=core_pair,
        curious_surge_pct=curious_surge_pct,
        parked_pair=parked_pair or core_pair,
    )

    return [
        {
            **r,
            "share_pct": 100.0 if r["pair"] == park else 0.0,
            "capital_quote": total_quote if r["pair"] == park else 0.0,
        }
        for r in scored
    ]


# ── routine ──────────────────────────────────────────────────────────────────


async def _pin_hunt(text: str) -> str:
    rows = split_kv(text)
    await pin_nest_sheet(
        title="Delta Raptor — nest hunt",
        slug="xrpl_mm_hunt_scorer",
        text=text,
        kpis=[
            ("Action", mark_of(rows, "action")),
            ("Mode", mark_of(rows, "hunting_mode")),
        ],
        heading="HUNT / NEST",
        blurb="Own hourly surge ≥50% hops. Fade → home. Idle cash never flies.",
    )
    return text


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    perch = compose_perch(
        config.hunting_mode,
        wallet=config.total_capital_quote,
        toehold=config.toehold_quote,
    )
    live = perch.live
    ranked = rank_pairs(config.pairs, config.fill_rate_ref, config.fill_prior, config.volume_weight)
    allocated = allocate_capital(
        ranked,
        live,
        config.min_share_pct,
        config.full_shift_dominance,
        config.base_weight,
        config.core_pair,
        config.curious_surge_pct,
        config.parked_pair or config.core_pair,
    )

    out: list[str] = []
    out.append("=== HUNT SCORES (opportunity x volume-surge / spread) ===")
    for r in ranked:
        tag = " [CORE]" if r["pair"] == config.core_pair else ""
        out.append(
            f"{r['pair']:14s} score {r['score']:8.2f}  headroom {r['headroom_bps']:6.2f}bps  "
            f"fill {r['fill_rate'] * 60:5.2f}/min  vol1h {r['volume_change_pct']:+6.1f}%  "
            f"viable={str(r['viable']).lower()} book={str(r['book_ok']).lower()}{tag}"
        )

    huntable = [a for a in allocated if a["score"] > 0]
    out.append("")
    out.append(f"=== CAPITAL ALLOCATION (hourly, live ${live:.0f} / wallet ${config.total_capital_quote:.0f}) ===")
    if perch.mode == HARVEST:
        out.append(
            f"hunting_mode: harvest — vol hunt still on; FULL_SHIFT moves ${live:.0f}; "
            f"idle ${perch.idle:.0f} stays off the book"
        )
    if not huntable:
        out.append("no huntable pair — no capital deployed")
        out.append("")
        out.append("=== VERDICT ===")
        out.append("action: HOLD — nothing viable, keep quoting widen mode only")
        return await _pin_hunt("\n".join(out))

    for a in allocated:
        out.append(
            f"{a['pair']:14s} share {a['share_pct']:6.1f}%  ${a['capital_quote']:8.2f}  (vol1h {a['volume_change_pct']:+6.1f}%)"
        )

    # Winner-take-all: park on core unless a pair FULL_SHIFTs.
    parked = next((a["pair"] for a in huntable if a["share_pct"] >= 99.9), config.core_pair)
    here = config.parked_pair or config.core_pair
    hop = plan_hop(here, parked, DEFAULT_BOOKS) if here != parked else None
    out.append("")
    out.append("=== VERDICT ===")
    if parked == config.core_pair and here == config.core_pair:
        out.append(f"action: HOLD — 100% on {config.core_pair} (core). Do not deploy XRP-USDC.")
        out.append(
            f"reason: no rotation nest's hourly volume is up ≥ {config.curious_surge_pct:.0f}% — stay home"
        )
        out.append(
            f"execute: deploy only the parked pair's controller at ${live:.0f}; "
            "all other pairs get total_amount_quote=0; re-evaluate next hour"
        )
    elif parked == config.core_pair:
        out.append(f"action: HOME — surge faded, live perch ${live:.0f} back to {config.core_pair}")
        out.append(
            f"reason: parked {here} no longer ≥ {config.curious_surge_pct:.0f}% hourly volume"
        )
        if hop:
            out.append(
                f"convert: {hop.side} {hop.receive_asset} on {hop.bridge_pair} "
                f"(pay {hop.pay_asset}) — one LIMIT this tick, then quote {parked} next hour"
            )
        out.append(
            f"execute: cancel {here}; park ${live:.0f} on {parked} after convert; "
            "idle stays off; re-evaluate next hour"
        )
    elif here == parked:
        out.append(f"action: STAY — still curious on {parked} (${live:.0f})")
        out.append(
            f"reason: {parked} hourly volume still ≥ {config.curious_surge_pct:.0f}%"
        )
        out.append(
            f"execute: keep the controller on {parked} at ${live:.0f}; "
            "all other pairs get total_amount_quote=0; re-evaluate next hour"
        )
    else:
        out.append(f"action: CURIOUS — live perch ${live:.0f} to {parked}")
        out.append(
            f"reason: {parked} hourly volume ≥ {config.curious_surge_pct:.0f}% "
            "(own surge, not vs RLUSD tape)"
        )
        if hop:
            out.append(
                f"convert: {hop.side} {hop.receive_asset} on {hop.bridge_pair} "
                f"(pay {hop.pay_asset}) — one LIMIT this tick, then quote {parked} next hour"
            )
            out.append(f"convert_note: {hop.note}")
        elif nest_tokens(here) != nest_tokens(parked):
            out.append("convert: NO PATH — stay on current nest; do not quote the target blind")
        out.append(
            f"execute: cancel {here}; after convert park ${live:.0f} on {parked}; "
            "idle stays off; re-evaluate next hour"
        )
    return await _pin_hunt("\n".join(out))
