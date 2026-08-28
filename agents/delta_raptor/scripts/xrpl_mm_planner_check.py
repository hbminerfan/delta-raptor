"""Read-only integration check for the XRPL maker quote planner.

Runs the planner routine's `run()` against the LIVE Hummingbot API + public
XRPL RPC. Places NO orders, creates NO executors — every call in the routine
is a read (candles, prices, amm_info, order book).

Usage:
    uv run python scripts/xrpl_mm_planner_check.py
    uv run python scripts/xrpl_mm_planner_check.py --pair USDC-RLUSD
    uv run python scripts/xrpl_mm_planner_check.py --pair OUSG-RLUSD \
        --amm-quote-issuer rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De \
        --amm-base-issuer roUSG6gvxKGBcgGVXhPKHEgy4YyDfiSi3

The script reports the planner's full verdict (reference price, floor, ceiling,
viability, sizing, book state) so a human can eyeball it before any dry-run
agent tick. Exit codes: 0 = ran; 1 = client/planner hard failure.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.delta_raptor.routines.xrpl_mm_quote_planner import Config, run as planner_run
from config_manager import get_client

# Verified issuer whitelist (RLUSD, USDC, and optional RWA books).
ISSUERS = {
    "RLUSD": "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De",
    "USDC": "rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE",
    "USDV": "rfffsukWALJB1PXYk7H8xkR6UJUDT8nMJE",
    "OUSG": "roUSG6gvxKGBcgGVXhPKHEgy4YyDfiSi3",
    "BBRL": "rH5CJsqvNqZGxrMyGaqLEoMWRYcVTAPZMt",
    "EUROP": "rMkEuRii9w9uBMQDnWV5AA43gvYZR9JxVK",
}

# Mirror the strategy's default_config (agents/delta_raptor/
# strategies/rlusd_xrp_maker/strategy.md).
STRATEGY_DEFAULTS = {
    "xrpl_pair": "RLUSD-XRP",
    "reference_connector": "binance_perpetual",
    "reference_pair": "XRP-USDT",
    "tick_interval_sec": 300,
    "requote_interval_sec": 30,  # controller executor_refresh_time — the exposure window
    "levels_per_side": 3,
    "total_amount_quote": 100.0,
    "adverse_k": 1.0,
    "use_vol_clock": True,
}


class _Ctx:
    """Minimal stand-in for the telegram context the routine touches.

    The routine calls ``get_client(context._chat_id, context=context)``; the
    config manager reads ``context.user_data`` for server preference (falls
    back to chat_defaults when empty) and ``context._chat_id`` for the chat.
    """

    _chat_id: int

    def __init__(self, chat_id: int):
        self._chat_id = chat_id
        self.user_data = {}


async def main(chat_id: int, pair: str, amm_quote_issuer: str, amm_base_issuer: str) -> int:
    client = await get_client(chat_id)
    if not client:
        print("FAIL: no Hummingbot server/client available for chat", chat_id)
        return 1

    base, _, quote = pair.partition("-")
    overrides = {
        "xrpl_pair": pair,
        "amm_quote_issuer": amm_quote_issuer or (ISSUERS.get(quote, "") if quote != "XRP" else ""),
        "amm_asset2_issuer": amm_base_issuer or (ISSUERS.get(base, "") if base != "XRP" else ""),
    }
    cfg = Config(**{**STRATEGY_DEFAULTS, **overrides})
    print(f"\nRunning planner: {cfg.xrpl_pair} vs {cfg.reference_pair} "
          f"@{cfg.reference_connector} (requote={cfg.requote_interval_sec}s, "
          f"levels={cfg.levels_per_side}, capital=${cfg.total_amount_quote})\n")
    print("─" * 72)
    try:
        result = await planner_run(cfg, _Ctx(chat_id))
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL: planner raised {type(exc).__name__}: {exc}")
        return 1
    print(result)
    print("─" * 72)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--chat-id", type=int, default=5587715073)
    ap.add_argument("--pair", default="RLUSD-XRP", help="BASE-QUOTE pair (default RLUSD-XRP)")
    ap.add_argument("--amm-quote-issuer", default="", help="Issuer of the quote asset (non-XRP)")
    ap.add_argument("--amm-base-issuer", default="", help="Issuer of the base asset (non-XRP)")
    args = ap.parse_args()
    sys.exit(asyncio.run(main(args.chat_id, args.pair, args.amm_quote_issuer, args.amm_base_issuer)))
