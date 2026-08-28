# Delta Raptor

Volume-hunting XRPL CLOB maker. Default nest `RLUSD-XRP`. Harvest (default) sits a
toehold and still hunts; race parks the envelope when volume is paid.

Drop `agents/delta_raptor/` into a Condor checkout.

```
./.venv/bin/python agents/delta_raptor/tests/validate_agent.py
./.venv/bin/pytest agents/delta_raptor/tests/ -q
```

Do not commit sessions, wallets, or `.env`.
