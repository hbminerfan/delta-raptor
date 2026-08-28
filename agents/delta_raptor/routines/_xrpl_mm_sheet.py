"""Pin a Condor dashboard sheet from a Delta Raptor planner dump.

Direct Python runs still group on the Routines page because ``source`` is
the routine slug. A failed pin never raises into the tick.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


def split_kv(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("==="):
            continue
        if ":" not in line:
            rows.append({"Key": "note", "Mark": line})
            continue
        key, val = line.split(":", 1)
        rows.append({"Key": key.strip(), "Mark": val.strip()})
    return rows


def mark_of(rows: list[dict[str, str]], *keys: str) -> str:
    want = {k.lower() for k in keys}
    for row in rows:
        if row["Key"].lower() in want:
            return row["Mark"]
    return "—"


async def pin_nest_sheet(
    *,
    title: str,
    slug: str,
    text: str,
    kpis: list[tuple[str, str]] | None = None,
    heading: str = "NEST / PLAN",
    blurb: str = "Harvest perch. Cup $100/$800 are ceilings.",
) -> None:
    try:
        from condor.reports import ReportBuilder

        rows = split_kv(text)
        builder = ReportBuilder(title)
        builder.source("routine", slug)
        builder.tags(["delta-raptor", "xrpl", "harvest", slug])
        for label, value in kpis or []:
            builder.kpi(label, value)
        builder.kpi("Pinned", datetime.now(timezone.utc).strftime("%H:%M:%S UTC"))
        builder.section(heading, blurb)
        builder.table(rows or [{"Key": "raw", "Mark": text[:800]}], ["Key", "Mark"])
        builder.manual_order()
        await builder.save()
    except Exception as exc:  # noqa: BLE001
        logger.warning("delta raptor nest sheet %s failed: %s", slug, exc)
