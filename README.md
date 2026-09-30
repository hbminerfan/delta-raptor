# Delta Raptor — Volume-Hunting Market Maker

![Delta Raptor — Volume-Hunting Market Maker](delta-raptor-cover-img.png)

A volume-hunting market maker on the XRPL native CLOB with **two hunting modes**.

- **Mode 1 · Race (Cup default):** the full **$800** envelope quotes RLUSD-XRP both sides. Hops only on a ≥ 300% own hourly surge — rare by design, since RLUSD-XRP is the only liquid book.
- **Mode 2 · Harvest (fallback):** **$100** live, **$700 idle**, SELL unlocks on fill.

Fair value from Binance XRP-USDT, never the ledger mid. Widen ~1% on a spike (Mode 2: the $100, not the envelope). Inventory turns only through the cheaper of CLOB vs native AMM, ≤ 25 bps, live perch only.

## What it is

- **Strategy type:** Agent — AI/autonomous trading agent (LLM tunes 1 action / 300s; Hummingbot `delta_raptor_pmm` quotes, no LLM in the quote loop)
- **Venue:** XRPL native DEX (CLOB) + native AMM rebalance
- **Core:** RLUSD-XRP (Ripple RLUSD `rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De`); rotation only after `FULL_SHIFT`
- **Tick:** 300s LLM tick; `delta_raptor_pmm` requotes every 5 min, or early when price drifts > 0.5% (checked every 60s, in code)
- **Envelope:** $800 ceiling — Race quotes all of it; Harvest parks $100, idle $700 off-book
- **Hedge:** off

## Run

```bash
./.venv/bin/python agents/delta_raptor/tests/validate_agent.py
./.venv/bin/pytest agents/delta_raptor/tests/ -q
```

Drop `agents/delta_raptor/` into a Condor checkout, and copy `agents/delta_raptor/controllers/delta_raptor_pmm.py` into the Hummingbot API `bots/controllers/market_making/`. Not a Hummingbot/Condor fork. Do not commit sessions, wallets, or `.env`.
