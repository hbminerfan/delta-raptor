# Organizer capital allocation — Delta Raptor ($800)

**Purpose:** make it obvious how to split the competition wallet so both arms can run.

## Hard split (finals dual-arm)

| Bucket | % of $800 | USD | Account / chain | Asset intent | Component |
|---|---|---|---|---|---|
| **Volume arm** | 70% | **560** | Binance spot (Hummingbot) | USD1 / stable pair liquidity for continuous desk turnover | `raptor_usd_desk` |
| **P&L arm** | 30% | **240** | XRPL wallet (Hummingbot `xrpl`) | RLUSD + XRP on RLUSD-XRP (and bridges only if hunt fires) | Condor `rlusd_xrp_maker` + `delta_raptor_pmm` |
| **P&L live toehold** | (of P&L) | **~80** | same XRPL wallet | Maximum resting quote size in harvest mode | `toehold_quote: 80` |
| **P&L idle** | (of P&L) | **~160** | same XRPL wallet | Held off-book until fill / rare hop | — |
| **Total** | 100% | **800** | two venues | — | — |

Constants live in code:

```python
# agents/delta_raptor/routines/_delta_raptor_alloc.py
RACE_ENVELOPE_USD = 800.0
VOLUME_ARM_USD = 560.0
PNL_ARM_USD = 240.0
TOEHOLD_USD = 80.0
```

Loop defaults (P&L arm):

- `total_amount_quote: 240`
- `total_capital_quote: 240`
- `hunting_mode: harvest`
- `toehold_quote: 80`
- `dual_arm: true`
- `volume_arm_usd: 560`
- `pnl_arm_usd: 240`
- `volume_controller: raptor_usd_desk`

## Funding steps for ops

1. Deposit **~$560** stable inventory to the **Binance** account connected as the volume desk.
2. Fund the **XRPL** bot wallet with **~$240** equivalent (RLUSD/XRP sufficient to quote RLUSD-XRP).
3. Confirm Hummingbot balances show both venues before start.
4. Start **volume desk** (~$560) and **P&L loop** (~$240) as two processes — not one $800 XRPL bot.

## What not to do

- Do **not** allocate $800 only to XRPL and expect dual-arm scoring.
- Do **not** set P&L `hunting_mode: race` or `total_amount_quote: 800` while the volume desk is also funded (double volume / wrong thesis).
- Do **not** sell the P&L sleeve into the volume desk.

## Optional race-only (legacy)

If organizers explicitly want single-arm XRPL race: set `hunting_mode: race`, `total_amount_quote: 800`, leave `raptor_usd_desk` stopped, and fund XRPL only. That is **not** the dual-arm finals package.
