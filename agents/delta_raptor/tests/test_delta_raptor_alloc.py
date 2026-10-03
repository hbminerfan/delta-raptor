"""Dual-arm $800 allocation constants stay organizer-clear."""
from agents.delta_raptor.routines._delta_raptor_alloc import (
    HUNTING_MODE_DEFAULT,
    ORGANIZER_BLURB,
    PNL_ARM_USD,
    RACE_ENVELOPE_USD,
    TOEHOLD_USD,
    VOLUME_ARM_USD,
    allocation_split,
    organizer_funding_table_md,
)


def test_envelope_split_sums_to_800():
    assert RACE_ENVELOPE_USD == 800.0
    assert VOLUME_ARM_USD + PNL_ARM_USD == RACE_ENVELOPE_USD
    assert VOLUME_ARM_USD == 560.0
    assert PNL_ARM_USD == 240.0
    assert TOEHOLD_USD == 80.0
    assert TOEHOLD_USD <= PNL_ARM_USD


def test_allocation_split_keys():
    d = allocation_split()
    assert d["hunting_mode"] == "harvest" == HUNTING_MODE_DEFAULT
    assert d["volume_controller"] == "raptor_usd_desk"
    assert d["pnl_loop"] == "rlusd_xrp_maker"
    assert d["funding"]["volume"]["usd"] == 560.0
    assert d["funding"]["pnl"]["usd"] == 240.0
    assert d["funding"]["pnl"]["live_toehold_usd"] == 80.0
    assert "560" in ORGANIZER_BLURB and "240" in ORGANIZER_BLURB
    table = organizer_funding_table_md()
    assert "$560" in table and "$240" in table and "$800" in table
