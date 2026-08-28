# Delta Raptor — isolated Condor agent

Drop-in Builders Cup entry. **Does not modify Condor engine or any other agent.**

```
agents/delta_raptor/          # copy this folder into the organizer Condor repo
  AGENT.md                    # BRAIN — identity, why, risk philosophy
  strategies/rlusd_xrp_maker/
    strategy.md               # HANDS — tick playbook, issuers, call shapes
    shutdown.md
  routines/                   # deterministic math (no LLM)
  skills/xrpl_mm_deploy/
  tests/                      # unit tests + validate_agent.py
  scripts/                    # optional live planner check
```

Official `agents/xrpl_market_maker/` is a **different** upstream agent. Leave it alone.

## Organizer sandbox

1. Copy `agents/delta_raptor/` into their Condor `agents/` directory.
2. Hummingbot API up, **xrpl** connector + wallet, a CEX feed for `XRP-USDT` mark
   (Binance perp by default). `custom_markets` must name the live books
   (`RLUSD-XRP`, `USDC-RLUSD`, `XRP-USDC`, …) — leftover `SOLO-XRP` only will
   503 on place.
3. **Condor web dashboard → Delta Raptor → RLUSD XRP Maker → Start New Session.**
   Agent chat is consult only; it does not start the tick loop.
4. Harvest is the default. Cup `$100` / `$800` are **ceilings**. The planner
   sizes to the live XRPL purse (~20%). A small organizer wallet is valid.
5. Override `agent_key` in `AGENT.md` or the session picker if their model
   roster differs. The playbook does not depend on a specific vendor.


Bot name is `delta_raptor-rlusd_xrp_maker` so FEAT-017 ownership cannot collide
with another agent.

## Checks (no trading)

```bash
# Loader / discovery
uv run python agents/delta_raptor/tests/validate_agent.py

# Pure-math unit tests
uv run pytest agents/delta_raptor/tests/ -q
```

## Layout

| File | Role | What lives here |
|---|---|---|
| `AGENT.md` | Brain | Who / why / philosophy / architecture. **No tick procedure.** |
| `strategy.md` | Hands | Every-tick steps, issuers, routine configs, sizes, errors. |
| `routines/*.py` | Hands (math) | Quote / hunt / rebalance verdicts. No orders. |

The LLM applies **one** routine verdict per tick. It never invents a price.
