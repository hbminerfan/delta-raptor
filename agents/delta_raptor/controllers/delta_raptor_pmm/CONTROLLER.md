---
type: market_making
description: pmm_simple plus coded price-drift requote — cancels resting quotes early when mid drifts > threshold, not only on the timer.
---

# delta_raptor_pmm

## What it is

`delta_raptor_pmm` is a thin extension of Hummingbot V2 `pmm_simple` /
`MarketMakingControllerBase`. Quoting, level ids, barriers and sizing are the
same as `pmm_simple`. The only addition is a **drift refresh** path in code
(no LLM):

1. **Timed refresh** — every `executor_refresh_time` seconds (default 300), an
   unfilled resting order is cancelled and requoted (inherited).
2. **Drift refresh** — every `drift_check_interval` seconds (default 60), each
   resting order's price is compared to the price the controller would quote
   now. If they differ by more than `drift_threshold_pct` (default 0.5%), that
   order is cancelled early and requoted on the next pass.

Filled / trading executors are never touched by either path.

### Category: market_making

- Base classes: `DeltaRaptorPMMController(MarketMakingControllerBase)`,
  `DeltaRaptorPMMConfig(MarketMakingControllerConfigBase)`.
- Lives under **`market_making/`** on the Hummingbot API; configs carry
  `controller_type: market_making` and `controller_name: delta_raptor_pmm`.

## Extra parameters

| Field | Default | Meaning |
|---|---|---|
| `drift_check_interval` | 60 | Seconds between drift checks |
| `drift_threshold_pct` | 0.005 | Early requote when order is this far from current target (0.005 = 0.5%) |

All other fields are the standard market-making / pmm_simple set
(`connector_name`, `trading_pair`, `total_amount_quote`, spreads, barriers,
`executor_refresh_time`, etc.).

## Deploy notes

- Package path must be `controllers/delta_raptor_pmm/delta_raptor_pmm.py`
  (directory form). A flat `controllers/delta_raptor_pmm.py` is skipped by Condor.
- Sync/upload as `market_making` / `delta_raptor_pmm`.
