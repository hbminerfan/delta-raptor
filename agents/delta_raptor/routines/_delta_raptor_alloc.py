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

# Human-facing one-liners for journals / dashboards / organizer docs
ORGANIZER_BLURB = (
    f"Fund ${VOLUME_ARM_USD:.0f} on Binance ({VOLUME_CONTROLLER}) and "
    f"${PNL_ARM_USD:.0f} on XRPL ({PNL_LOOP}, toehold ${TOEHOLD_USD:.0f}). "
    f"Total ${RACE_ENVELOPE_USD:.0f}. Do not put all ${RACE_ENVELOPE_USD:.0f} on XRPL."
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
