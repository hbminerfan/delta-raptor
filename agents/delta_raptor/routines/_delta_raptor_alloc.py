"""Delta Raptor dual-arm capital split ($800 entry).

Organizer funding (Builders Cup finals)
---------------------------------------
Total envelope: $800

| Arm    | Pct | USD | Venue                         | Component            |
|--------|-----|-----|-------------------------------|----------------------|
| Volume | 70% | 560 | Binance USD1 / stable desk    | raptor_usd_desk      |
| P&L    | 30% | 240 | XRPL RLUSD-XRP harvest MM     | rlusd_xrp_maker loop |
| Toehold|  —  |  80 | XRPL live quote (within P&L)  | toehold_quote        |

Do not put the full $800 on XRPL when dual-arm is active.
Canonical numbers — keep README / ORGANIZER_CAPITAL.md / loop frontmatter in sync.
"""
from __future__ import annotations

RACE_ENVELOPE_USD = 800.0
VOLUME_ARM_USD = 560.0  # 70% — raptor_usd_desk (Binance)
PNL_ARM_USD = 240.0  # 30% — XRPL harvest loop
TOEHOLD_USD = 80.0  # live P&L quote inside PNL_ARM_USD
VOLUME_CONTROLLER = "raptor_usd_desk"
PNL_LOOP = "rlusd_xrp_maker"
PNL_CONTROLLER = "delta_raptor_pmm"
HUNTING_MODE_DEFAULT = "harvest"
VOLUME_ARM_PCT = VOLUME_ARM_USD / RACE_ENVELOPE_USD
PNL_ARM_PCT = PNL_ARM_USD / RACE_ENVELOPE_USD

# Entry-level stop: 10% of the full $800 race envelope — NOT 10% of the $240 P&L sleeve.
# $80 combined (or either-arm absolute) loss trips stop / retire. Do not scale this to pnl_arm_usd.
ENTRY_STOP_LOSS_PCT = 0.10
ENTRY_STOP_LOSS_USD = RACE_ENVELOPE_USD * ENTRY_STOP_LOSS_PCT  # 80.0
# Optional trail: arm after +$40 (5% of envelope), trail width $40 (5%).
ENTRY_TRAIL_ARM_USD = RACE_ENVELOPE_USD * 0.05  # 40.0
ENTRY_TRAIL_WIDTH_USD = RACE_ENVELOPE_USD * 0.05  # 40.0

# Human-facing one-liners for journals / dashboards / organizer docs
ORGANIZER_BLURB = (
    f"Fund ${VOLUME_ARM_USD:.0f} on Binance ({VOLUME_CONTROLLER}) and "
    f"${PNL_ARM_USD:.0f} on XRPL ({PNL_LOOP}, toehold ${TOEHOLD_USD:.0f}). "
    f"Total ${RACE_ENVELOPE_USD:.0f}. Do not put all ${RACE_ENVELOPE_USD:.0f} on XRPL. "
    f"Entry stop-loss ${ENTRY_STOP_LOSS_USD:.0f} (10% of ${RACE_ENVELOPE_USD:.0f}), not 10% of the P&L sleeve."
)


def allocation_split() -> dict:
    """Return the dual-arm split for configs, tests, and organizer tooling."""
    return {
        "total_envelope_usd": RACE_ENVELOPE_USD,
        "volume_arm_usd": VOLUME_ARM_USD,
        "pnl_arm_usd": PNL_ARM_USD,
        "toehold_usd": TOEHOLD_USD,
        "volume_arm_pct": VOLUME_ARM_PCT,
        "pnl_arm_pct": PNL_ARM_PCT,
        "volume_controller": VOLUME_CONTROLLER,
        "pnl_loop": PNL_LOOP,
        "pnl_controller": PNL_CONTROLLER,
        "hunting_mode": HUNTING_MODE_DEFAULT,
        "entry_stop_loss_pct": ENTRY_STOP_LOSS_PCT,
        "entry_stop_loss_usd": ENTRY_STOP_LOSS_USD,
        "entry_trail_arm_usd": ENTRY_TRAIL_ARM_USD,
        "entry_trail_width_usd": ENTRY_TRAIL_WIDTH_USD,
        "organizer_blurb": ORGANIZER_BLURB,
        "funding": {
            "volume": {
                "usd": VOLUME_ARM_USD,
                "pct": VOLUME_ARM_PCT,
                "venue": "binance",
                "purpose": "stable desk volume / turnover",
                "component": VOLUME_CONTROLLER,
            },
            "pnl": {
                "usd": PNL_ARM_USD,
                "pct": PNL_ARM_PCT,
                "venue": "xrpl",
                "purpose": "RLUSD-XRP harvest market making",
                "component": PNL_LOOP,
                "live_toehold_usd": TOEHOLD_USD,
            },
        },
    }


def organizer_funding_table_md() -> str:
    """Markdown table for README / Botcamp packets."""
    return (
        "| Arm | Share | Amount | Venue | Component |\n"
        "|---|---|---|---|---|\n"
        f"| **Volume** | {VOLUME_ARM_PCT:.0%} | **${VOLUME_ARM_USD:.0f}** | Binance stable desk | `{VOLUME_CONTROLLER}` |\n"
        f"| **P&L** | {PNL_ARM_PCT:.0%} | **${PNL_ARM_USD:.0f}** | XRPL RLUSD-XRP | `{PNL_LOOP}` + `{PNL_CONTROLLER}` |\n"
        f"| Toehold (P&L live) | — | **${TOEHOLD_USD:.0f}** | XRPL (within P&L) | `toehold_quote` |\n"
        f"| **Total** | 100% | **${RACE_ENVELOPE_USD:.0f}** | dual venue | dual-arm |\n"
    )


def entry_stop_loss_usd(envelope_usd: float | None = None, pct: float | None = None) -> float:
    """Dollar stop from full entry envelope (default $800 x 10% = $80).

    Always base on the race envelope, never on pnl_arm_usd alone.
    """
    env = float(RACE_ENVELOPE_USD if envelope_usd is None else envelope_usd)
    p = float(ENTRY_STOP_LOSS_PCT if pct is None else pct)
    return env * p


def volume_desk_drawdown_ceiling_usd() -> float:
    """raptor_usd_desk drawdown_ceiling_usd — same $80 entry stop."""
    return float(ENTRY_STOP_LOSS_USD)


def pnl_max_global_drawdown_quote() -> float:
    """XRPL deploy max_global_drawdown_quote — same $80 entry stop (not 12% of $240)."""
    return float(ENTRY_STOP_LOSS_USD)
