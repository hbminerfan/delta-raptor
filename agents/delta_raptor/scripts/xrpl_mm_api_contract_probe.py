"""Read-only probe: hummingbot-api controller surface vs strategy contract.

Checks (no writes):
1. Available market_making controllers on the live API
2. pmm_simple config template — are the strategy's override fields in the schema?
3. validate_controller_config with the strategy's exact overrides
"""
import asyncio
import json
import sys

sys.path.insert(0, ".")
from config_manager import get_config_manager

# From agents/delta_raptor/strategies/rlusd_xrp_maker/strategy.md +
# deploy skill Phase 3 overrides.
STRATEGY_CONFIG = {
    "id": "rlusd-xrp-maker",
    "controller_type": "market_making",
    "controller_name": "pmm_simple",
    "executor_refresh_time": 30,
    "skip_rebalance": True,
    "buy_spreads": [0.000359, 0.000573, 0.000786],
    "sell_spreads": [0.000359, 0.000573, 0.000786],
    "total_amount_quote": 99.7606,
    "leverage": 1,
    "connector_name": "xrpl",
    "trading_pair": "RLUSD-XRP",
}


async def main() -> int:
    cm = get_config_manager()
    client = await cm.get_client()
    ctl = client.controllers

    try:
        lst = await ctl.list_controllers()
        print("Available controllers:", json.dumps(lst)[:500])
    except Exception as exc:
        print("list_controllers FAILED:", type(exc).__name__, str(exc)[:200])

    try:
        tpl = await ctl.get_controller_config_template("market_making", "pmm_simple")
        print("\npmm_simple template keys:", list(tpl.keys())[:25] if isinstance(tpl, dict) else type(tpl))
        tpl_keys = set(tpl.keys()) if isinstance(tpl, dict) else set()
        for k in ("executor_refresh_time", "skip_rebalance", "buy_spreads",
                  "sell_spreads", "total_amount_quote", "leverage",
                  "manual_kill_switch", "connector_name", "trading_pair"):
            print(f"  '{k}': {'PRESENT' if k in tpl_keys else 'MISSING from template'}")
    except Exception as exc:
        print("template FAILED:", type(exc).__name__, str(exc)[:200])

    try:
        verdict = await ctl.validate_controller_config(
            STRATEGY_CONFIG["controller_type"], STRATEGY_CONFIG["controller_name"], STRATEGY_CONFIG
        )
        print("\nvalidate_controller_config:", json.dumps(verdict)[:400])
    except Exception as exc:
        print("validate FAILED:", type(exc).__name__, str(exc)[:200])

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
