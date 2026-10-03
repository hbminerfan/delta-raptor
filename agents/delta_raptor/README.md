# Delta Raptor — isolated Condor agent (dual-arm)

Drop-in Builders Cup entry. **Does not modify Condor engine or any other agent.**

## Organizer capital ($800) — read first

**Entry stop-loss: $80 (10% of $800 total), not 10% of the $240 P&L arm.**

| Arm | Amount | Where | Component |
|---|---|---|---|
| **Volume** | **~$560 (70%)** | Binance USD1 stable desk | `controllers/generic/raptor_usd_desk.py` |
| **P&L** | **~$240 (30%)** | XRPL RLUSD-XRP harvest | this agent + `controllers/delta_raptor_pmm/` |
| Toehold (P&L live) | **~$80** | XRPL | `toehold_quote` inside the $240 sleeve |

Full table: repo root `ORGANIZER_CAPITAL.md` and `routines/_delta_raptor_alloc.py`.

**Default:** `hunting_mode: harvest` — not full-envelope race.

```
agents/delta_raptor/
  AGENT.md
  README.md
  loops/rlusd_xrp_maker/          # P&L tick playbook
  routines/                       # deterministic math (+ _delta_raptor_alloc.py)
  controllers/delta_raptor_pmm/   # XRPL quoter package
  controllers/generic/raptor_usd_desk.py  # volume arm
  skills/xrpl_mm_deploy/
  tests/
  scripts/
```

Official `agents/xrpl_market_maker/` is a **different** upstream agent. Leave it alone.

## Organizer sandbox

1. Copy `agents/delta_raptor/` into Condor `agents/`.
2. Hummingbot API up with:
   - **xrpl** + wallet funded **~$240** (P&L)
   - **binance** (or desk connector) funded **~$560** (volume)
   - CEX mark for `XRP-USDT` (`binance_perpetual` by default)
   - `custom_markets` must include live XRPL books (`RLUSD-XRP`, …)
3. Install controllers:
   - `controllers/delta_raptor_pmm/` → Hummingbot market-making / packaged controllers path
   - `controllers/generic/raptor_usd_desk.py` → `controllers/generic/`
4. **Volume arm:** start `raptor_usd_desk` bot (see repo `conf/raptor_usd_desk.sample.yml`), sized ~$560.
5. **P&L arm:** Condor web → **Delta Raptor → RLUSD XRP Maker → Start**.  
   Defaults: `total_amount_quote: 240`, `hunting_mode: harvest`, `toehold_quote: 80`.  
   Agent chat is consult only; it does not start the tick loop.
6. Override `agent_key` only if the model roster differs.

Bot name is `delta_raptor-rlusd_xrp_maker` so FEAT-017 ownership cannot collide with another agent.

## Checks (no trading)

```bash
uv run python agents/delta_raptor/tests/validate_agent.py
uv run pytest agents/delta_raptor/tests/ -q
```

## Layout

| File | Role |
|---|---|
| `AGENT.md` | Brain — identity, dual-arm why. **No tick procedure.** |
| `loop.md` | Hands — tick steps, issuers, sizes (P&L $240 harvest). |
| `routines/_delta_raptor_alloc.py` | Canonical $800 / $560 / $240 / $80 split. |
| `routines/*.py` | Quote / hunt / rebalance math. No orders. |
| `raptor_usd_desk.py` | Volume arm controller (~$560). |

The LLM applies **one** routine verdict per tick. It never invents a price.
