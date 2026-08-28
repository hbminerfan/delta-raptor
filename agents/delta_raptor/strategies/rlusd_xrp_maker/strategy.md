---
name: RLUSD XRP Maker
description: >-
  Tick playbook for Delta Raptor: start 100% on RLUSD-XRP, harvest $100 live /
  $700 idle by default. Hop to a rotation nest when *that pair's* hourly volume
  is up ≥ 50%. Fade → home. Convert leftovers on a whitelist bridge first.
agent_key: null
skills:
- xrpl_mm_deploy
default_config:
  frequency_sec: 300
  execution_mode: loop
  total_amount_quote: 800
  bot_mode: bot
  bot_name: delta_raptor-rlusd_xrp_maker
  xrpl_pair: RLUSD-XRP
  reference_connector: binance_perpetual
  reference_pair: XRP-USDT
  levels_per_side: 3
  executor_refresh_time: 30
  skip_rebalance: true
  adverse_k: 1.0
  use_vol_clock: true
  amm_asset2_issuer: rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De
  amm_asset2_currency: RLUSD
  inventory_target_pct: 50
  inventory_band_pct: 1
  rebalance_enabled: true
  max_rebalance_cost_bps: 25
  max_rebalance_amount_quote: 50
  rebalance_progressive_frac: 0.5
  total_capital_quote: 800
  min_share_pct: 0
  full_shift_dominance: 3.0
  volume_weight: 1.0
  widen_enabled: true
  widen_distance_pct: 1.0
  quote_mode: top_of_book
  top_of_book_improve_pct: 0.01
  hunting_mode: harvest
  toehold_quote: 100
  curious_surge_pct: 50
  parked_pair: RLUSD-XRP
  hedge_enabled: false
  hedge_connector: binance_perpetual
  hedge_pair: XRP-USDT
  risk_limits:
    max_position_size_quote: 800
    max_open_executors: 8
    max_drawdown_pct: 10
    shutdown_drawdown_pct: 20
default_trading_context: 'Trade RLUSD-XRP on xrpl; hop on own hourly surge ≥50%; convert then quote; reference binance_perpetual XRP-USDT'
created_by: 0
created_at: '2026-07-28T00:00:00Z'
---

# RLUSD XRP Maker

> **Identity, edge and risk philosophy live in AGENT.md.** This playbook is the
> exact procedure for every tick — issuers, routine calls, sizes, exits.
> Follow it. Routines emit the numbers; you apply one verdict.

Read every runtime value from `[CURRENT CONFIG]`. Never hardcode a venue in a
routine. Connector is `xrpl`. Bot name **must** stay under the ownership
namespace `delta_raptor-rlusd_xrp_maker` (do not use `rlusd-xrp-maker`).

## Pair universe (verified issuers — this file is the source)

| Role | Pair | Quote | Base issuer | Quote issuer (non-XRP) | Notes |
|---|---|---|---|---|---|
| **Primary (core)** | RLUSD-XRP | XRP | RLUSD `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De` | — | deepest book (0.11%), AMM 0.21% |
| Rotation | USDC-RLUSD | RLUSD | USDC `rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE` | RLUSD `rMxCKbED…` | live, AMM 0.14% |
| Rotation | BBRL-RLUSD | RLUSD | BBRL `rH5CJsqvNqZGxrMyGaqLEoMWRYcVTAPZMt` | RLUSD | thin (3% book, AMM 1.0%) |
| Rotation | EUROP-XRP | XRP | EUROP `rMkEuRii9w9uBMQDnWV5AA43gvYZR9JxVK` | — | junk-filled, AMM 0.27% |
| Rotation | XRP-USDC | USDC | — | USDC `rGm7WCVp9gb4jZHWTEtGUr4dd74z2XuWhE` | AMM 0.20% |
| Rotation | EUROP-RLUSD | RLUSD | EUROP `rMkEuRii…` | RLUSD | thin book |
| Watchlist | USDV-XRP | XRP | USDV `rfffsukWALJB1PXYk7H8xkR6UJUDT8nMJE` | — | no book/AMM — do not quote |
| Watchlist | OUSG-RLUSD | RLUSD | OUSG `roUSG6gvxKGBcgGVXhPKHEgy4YyDfiSi3` | RLUSD | no book/AMM — do not quote |

- **Issuer whitelist is mandatory.** Never quote an issuer not in this table.
  Copycats: BUIDL (fake BlackRock), EUROP-2 `rEuRoPiQJ…` (fake Schuman), GOLD, XAU.
- Re-verify `account_info.Domain` matches the official project domain before deploy.
- Per-pair reference: invert Binance XRP-USDT for XRP-quoted pairs. RLUSD-quoted
  stables may use ≈1.0 implied unless a pair-specific CEX feed exists.

## Launch

- Start from the **Condor dashboard**: Agent **Delta Raptor** → strategy
  **RLUSD XRP Maker** → **Start New Session**. Chat does not quote.
- `frequency_sec` = LLM tick. `executor_refresh_time` = quote exposure.
  Planner floor uses the **latter** in controller mode.
- `bot_mode: bot` + `bot_name: delta_raptor-rlusd_xrp_maker` ⇒ controller mode
  inside the ownership namespace. Do not clear `bot_name`.
- First tick, if no bot by that name: deploy `pmm_simple` (see Guardrails /
  `xrpl_mm_deploy` skill). Fall back to executors only after a real controller
  failure — then journal why. Do **not** clear `bot_name` to pick executor mode
  unless that failure is recorded.
- Harvest live size comes from the planner (`controller_total_amount_quote`),
  not from `total_amount_quote: 800` in this file. That 800 is a ceiling.

## Startup guard — notSynced

XRPL connector can 500/`notSynced` on tick #1–#2.

- Tick #1/#2 XRPL error → **HOLD entire tick**. No deploy, no cancel, no failure journal.
- Tick #3+ still failing → journal `category="execution"`.
- CEX reference errors are **hard stops** immediately.

## Each tick

MCP is pre-attached. **Do not ToolSearch.** First call `manage_bots` or
`manage_routines`. A ToolSearch catalog of WebFetch/Monitor is not a missing-MCP HOLD.

**0. Hunt (hourly)** — winner-take-all capital.

Run the quote planner on **core + any rotation pair you will score** (not watchlist
without depth). Feed planner outputs + own fills (30 min) + hourly volume change
into the hunt scorer:

```
manage_routines(action="run", name="xrpl_mm_hunt_scorer",
                strategy_id="delta_raptor.rlusd_xrp_maker",
                config={"pairs": [
                    {"pair": "<active>", "viable": <planner>, "book_ok": <book present>,
                     "headroom_bps": <planner headroom>, "book_spread_bps": <planner book spread>,
                     "fills_window": <own fills last 30min>, "window_sec": 1800,
                     "volume_change_pct": <hourly volume change %>}, ...],
                    "core_pair": "RLUSD-XRP",
                    "total_capital_quote": <wallet 800>,
                    "toehold_quote": <config>,
                    "hunting_mode": <config>,
                    "curious_surge_pct": <config>,
                    "parked_pair": <currently quoting nest>,
                    "min_share_pct": 0,
                    "full_shift_dominance": <unused, kept for compat>})
```

- **Startup / no surge: 100% RLUSD-XRP.** `total_amount_quote=0` on every other pair.
- **CURIOUS hop** when a *rotation* pair's own hourly `volume_change_pct` ≥
  `curious_surge_pct` (default **50**). RLUSD having more tape does **not** veto.
  Live perch only (harvest `$100`). Idle stays off.
- **HOME** next hour if that surge is gone. Back to RLUSD-XRP.
- **CONVERT before quoting the new nest.** One LIMIT this tick on the whitelist
  bridge, then quote next hour. Example: RLUSD-XRP → XRP-USDC → **BUY USDC on
  `USDC-RLUSD`** (pay RLUSD). Reverse hop: **SELL USDC** on the same book.
  If `convert: NO PATH`, do not quote the target.
- Re-evaluate once per hour. Journal the score table + verdict.

**1. Plan** (the parked pair — issuers from the table, never guessed)

```
manage_routines(action="run", name="xrpl_mm_quote_planner",
                strategy_id="delta_raptor.rlusd_xrp_maker",
                config={"xrpl_pair": "<parked pair>",
                        "reference_connector": "<from config>",
                        "reference_pair": "<from config, pair-appropriate>",
                        "tick_interval_sec": <frequency_sec>,
                        "requote_interval_sec": <executor_refresh_time in controller mode,
                                                 else frequency_sec>,
                        "levels_per_side": <from config>,
                        "total_amount_quote": <planner live — harvest $100, never wallet $800>,
                        "hunting_mode": <config>,
                        "wallet_ceiling_quote": <config total_capital_quote>,
                        "toehold_quote": <config>,
                        "inv_sellable_usd": <RLUSD inventory marked USD, 0 on a fresh seed>,
                        "adverse_k": <from config>,
                        "use_vol_clock": <from config>,
                        "amm_quote_issuer": "<quote issuer if quote != XRP, else ''>",
                        "amm_asset2_issuer": "<base issuer if base != XRP, else ''>"})
```

Tick #1/#2 `notSynced` / `status: ERROR` → startup guard (HOLD).

**2. Viability → WIDEN, never stop**

- `viable: true` → top-of-book. Harvest: post **BUY** if `buy_live_usd` > 0; post **SELL**
  if `sell_live_usd` > 0. One-sided is allowed. If XRP is locked and RLUSD is free, that
  is a SELL seed — not a $100 BUY.
- Planner `hold: true` → **HOLD** this tick.
- `viable: false` (correct requote interval) → **WIDEN**: one level at
  `widen_distance_pct` (1%) from mid, sized at **live perch**. Harvest: do **not**
  rest $800. Controller: `widen_spread_fraction`.
  Executor fallback: one LIMIT_MAKER BUY at `widen_bid` until SELL is armed.
  **Do not kill the switch.**
- Empty / unavailable book → HOLD (no quotes at all). That is the only hard stop.
- Large `divergence_vs_reference_bps` → stale; act only if it persists two ticks.

**3. Inventory + AMM rebalancing** (if `rebalance_enabled` and share outside
`inventory_target_pct ± inventory_band_pct`)

```
manage_routines(action="run", name="xrpl_mm_rebalance_planner",
                strategy_id="delta_raptor.rlusd_xrp_maker",
                config={"xrpl_pair": "<pair>",
                        "base_issuer": "<base issuer or '' if XRP>",
                        "quote_issuer": "<quote issuer or '' if XRP>",
                        "base_balance": <base>, "quote_balance": <quote>,
                        "base_price_quote": <CEX reference in quote units>,
                        "inventory_target_pct": <config>, "inventory_band_pct": <config>,
                        "max_rebalance_cost_bps": <config>,
                        "max_rebalance_amount_quote": <config>,
                        "progressive_frac": <config>,
                        "book_spread_bps": <from planner/book, else omit>})
```

- Size: progressive half-step of excess, cap `max_rebalance_amount_quote`.
- Execute **one** rebalance action this tick:
  - `REBALANCE_AMM` → `manage_amm(action="execute_swap", ...)` using the planner's
    `execute:` line. `quote_swap` first. Slippage 0.5%. Never exceed planner amount.
  - `REBALANCE_CLOB` → LIMIT that crosses the book. Never MARKET.
  - `HOLD` → journal why.

**4. One action**

*Controller (`bot_name` in namespace) — tune only; bot quotes:*

| Condition | Action |
|---|---|
| notSynced (tick #1/#2) | **HOLD** |
| Book unavailable | **HOLD** — do not quote blind |
| `viable: false` | **WIDEN** — push `widen_spread_fraction`, keep quoting |
| Viable, previously widened/killed | **RESUME** — restore top-of-book spreads |
| Viable, plan changed | **RETUNE** — both config stores |
| Viable, unchanged | **HOLD** |
| Hunt CURIOUS | **CONVERT then MOVE** — bridge LIMIT this tick; quote the new nest next hour. Idle stays off. |
| Hunt HOME | **CONVERT then MOVE** back to RLUSD-XRP. |
| Inventory outside band | **REBALANCE** via planner verdict (never `skip_rebalance: false`) |
| `hedge_enabled`, delta outside band | **HEDGE** |
| Hedge on, one leg missing | **FIX LEG PARITY** — only action this tick |

*Executor fallback (only after recorded controller failure):*

| Condition | Action |
|---|---|
| notSynced (tick #1/#2) | **HOLD** |
| Book unavailable | **HOLD** — cancel |
| `viable: false` | **WIDEN** — one LIMIT_MAKER per side |
| Viable, no offers | **QUOTE** — LIMIT_MAKER at planner levels |
| Viable, reference moved > ½ spread | **REQUOTE** |
| Viable, stable | **HOLD** |

**5. Journal** the single action + the routine verdict strings.
Execution failures → `category="execution"`.

## Sizing

- Free balance only (1 XRP + 0.2 × open offers).
- Harvest live = min(`toehold_quote`, 20% of **observed XRPL purse**, free-balance room).
  Cup $100 / $800 are **ceilings**. An organizer ~$85 book lives ~$17, not $100, not $800.
- If available XRP cannot cover reserve + a BUY, harvest **SELLs RLUSD** it already holds
  (one-sided seed). Do not BUY-blind into negative available XRP.
- `hold: true` → **HOLD**. Do not upsert a controller.
- **Never upsert `pmm_simple.total_amount_quote` above planner `controller_total_amount_quote`.**
- Race live = min(envelope, observed purse). Organizer smoke stays **harvest**.

## Guardrails

- No XRPL candles. No `place_order`. Fair value from `reference_connector` only.
- Prices = planner top-of-book (0.01% better than best bid/ask). Controller
  reference = **book mid** + `controller_top_of_book_spreads`.
- Keep `skip_rebalance: true` on the controller (agent owns rebalance).
- `pmm_simple` only (no `pmm_dynamic` — XRPL has no candles).
  Override: `executor_refresh_time=30`, `skip_rebalance=true`, `leverage=1`,
  triple-barrier fields `null`.
- Retunes update **both** controller stores. Executors pass `controller_id="{agent_id}"`.
- Declare `max_global_drawdown_quote` on every deploy.

## Errors

| Tell | Action |
|---|---|
| `notSynced` / 500 on tick #1 or #2 | **HOLD** — retry next tick |
| `notSynced` / 500 on tick #3+ | Journal `category="execution"`, notify |
| `tecUNFUNDED_OFFER` | Resize vs free balance, retry once |
| `tecNO_LINE` / `tecPATH_DRY` | Notify — do not retry blindly |
| Accepted but absent from book | Check balances before replacing |
| Reference feed down | HOLD / cancel — never quote blind |

On create failure: re-fetch schema, fix, retry **once**, journal. No retry loops in a tick.
