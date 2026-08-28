"""Condor discovers only the three planners; helper _*.py files are skipped."""
from pathlib import Path

from routines.base import discover_routines_from_path

ROUTINES = Path(__file__).resolve().parents[1] / "routines"
EXPECTED = {
    "xrpl_mm_quote_planner",
    "xrpl_mm_hunt_scorer",
    "xrpl_mm_rebalance_planner",
}


def test_condor_loader_finds_exactly_the_three_planners():
    found = discover_routines_from_path(ROUTINES, agent_slug="delta_raptor", force_reload=True)
    assert set(found) == EXPECTED
    for name in EXPECTED:
        info = found[name]
        assert info.config_class is not None
        assert info.run_fn is not None
