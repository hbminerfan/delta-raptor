"""Validate Delta Raptor against Condor's real loaders (no network, no trading)."""
import inspect
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

ok = True

from routines.base import discover_routines_from_path

rdir = REPO / "agents/delta_raptor/routines"
found = discover_routines_from_path(rdir, agent_slug="delta_raptor")
print("=== ROUTINE DISCOVERY ===")
for name in sorted(found):
    info = found[name]
    cat = getattr(info, "category", None) or getattr(getattr(info, "module", None), "CATEGORY", "?")
    fields = list(info.config_class.model_fields) if getattr(info, "config_class", None) else []
    print(f"  [OK] {name:<28} category={cat:<12} fields={len(fields)}")
for expected in ("xrpl_mm_quote_planner", "xrpl_mm_hunt_scorer", "xrpl_mm_rebalance_planner"):
    if expected not in found:
        print(f"  [FAIL] {expected} NOT discovered")
        ok = False

print("\n=== ROUTINE CONTRACT ===")
VALID = {"Market Data", "Analysis", "Arbitrage", "Monitoring"}
for name, info in sorted(found.items()):
    has_run = inspect.iscoroutinefunction(info.run_fn)
    cat = info.category
    has_cfg = info.config_class is not None
    good = has_run and has_cfg and cat in VALID
    ok &= good
    print(f"  [{'OK' if good else 'FAIL'}] {name:<28} async={has_run} Config={has_cfg} CATEGORY={cat!r}")

print("\n=== AGENT / STRATEGY LOADING ===")
os.chdir(REPO)
from condor.agents.agent import AgentStore
from condor.agents.ownership import bot_namespace, in_namespace
from condor.agents.strategy import StrategyStore
from condor.frontmatter import slugify as _slugify

agent = AgentStore().get("delta_raptor")
if not agent:
    print("  [FAIL] agent 'delta_raptor' not loaded")
    ok = False
else:
    print(f"  [OK] agent slug={agent.slug} key={agent.agent_key}")
    print(f"       tools={len(agent.tools)} server_required={agent.server_required}")
    leak = any("xrpl_market_maker" in t for t in agent.tools)
    if leak:
        print("  [FAIL] tools still reference xrpl_market_maker")
        ok = False

official = AgentStore().get("xrpl_market_maker")
if official:
    print(f"  [OK] official xrpl_market_maker still loads separately (slug={official.slug})")
    if official.slug == "delta_raptor":
        print("  [FAIL] slugs collided")
        ok = False

strats = [s for s in StrategyStore().list_all() if s.agent_slug == "delta_raptor"]
if not strats:
    print("  [FAIL] no strategy loaded for delta_raptor")
    ok = False
for s in strats:
    print(f"  [OK] strategy key={s.key} name={s.name}")
    bot = (s.default_config or {}).get("bot_name", "")
    ns = bot_namespace(s.agent_slug, s.slug)
    owned = in_namespace(bot, ns) if bot else False
    print(f"       bot_name={bot!r} namespace={ns} in_namespace={owned}")
    if bot and not owned:
        print("  [FAIL] bot_name outside ownership namespace — organizer deploy will be refused")
        ok = False
    exp = _slugify(s.name)
    d = (REPO / "agents/delta_raptor/strategies" / exp).is_dir()
    ok &= d
    print(f"  [{'OK' if d else 'FAIL'}] folder '{exp}' matches slugified name")

# Isolation: official strategy must not import delta_raptor
print("\n=== ISOLATION ===")
import ast

def imports_of(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text())
    except Exception:
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
        elif isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out

for py in (REPO / "agents/delta_raptor").rglob("*.py"):
    mods = imports_of(py)
    bad = [m for m in mods if m.startswith("agents.") and not m.startswith("agents.delta_raptor")]
    # config_manager / telegram / httpx are platform, not other agents
    if bad:
        print(f"  [FAIL] {py.relative_to(REPO)} imports {bad}")
        ok = False
if ok:
    print("  [OK] delta_raptor python files do not import other agents")

print("\n=== RESULT:", "ALL CHECKS PASSED" if ok else "FAILURES PRESENT", "===")
sys.exit(0 if ok else 1)
