"""F1 premarket filter smoke test.

Runs the same candidate flow as the production F1 module, but without touching
DB/state: ranking API -> expected execution enrichment -> gap filter ->
liquidity top 10%.

  python api_tests/f1.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import api_tests._helper as h


async def run() -> tuple[bool, list[dict]]:
    """Return (ok, liquidity-filtered candidates)."""
    h.header("F1. premarket filter  ranking + expected execution")

    from src.api import auth
    import src.modules.f1_filter as mod

    if not await auth.load_or_refresh():
        h.fail("token")
        return False, []

    try:
        raw = await mod._fetch_all_premarket()
    except Exception as e:
        h.fail("_fetch_all_premarket", repr(e))
        return False, []

    gap_filtered = mod._filter_by_gap(raw)
    gap_filtered.sort(key=lambda c: c.get("avg_amount_5d", 0.0), reverse=True)
    threshold = max(1, int(len(gap_filtered) * mod.LIQUIDITY_TOP_PCT)) if gap_filtered else 0
    candidates = gap_filtered[:threshold]

    print(
        f"  raw candidates: {len(raw)}"
        f"  gap pass({mod.GAP_MIN*100:.0f}~{mod.GAP_MAX*100:.0f}%): {len(gap_filtered)}"
        f"  liquidity pass: {len(candidates)}"
    )
    print()
    print("  ticker  gap(final) band          reason             rank_gap  exp_gap   price      qty      amount")
    print("  " + "-" * 112)
    for c in raw[:20]:
        exp_gap = c.get("expected_api_gap_pct")
        print(
            f"  {c['ticker']:<6}"
            f"  {c['gap_pct']*100:>+8.2f}%"
            f"  {c.get('gap_band',''):<12}"
            f"  {c.get('gap_reason',''):<18}"
            f"  {c.get('ranking_gap_pct',0)*100:>+8.2f}%"
            f"  {(exp_gap * 100) if exp_gap is not None else 0:>+7.2f}%"
            f"  {c['expected_price']:>8,.0f}"
            f"  {c.get('expected_qty',0):>7,d}"
            f"  {c.get('expected_amount',0)/1e8:>8.1f}억"
        )
    if len(raw) > 20:
        print(f"  ... plus {len(raw)-20} rows")

    print(f"\n  liquidity top {mod.LIQUIDITY_TOP_PCT*100:.0f}% after gap filter")
    if not candidates:
        print("    (none)")
    for c in candidates:
        print(
            f"    {c['ticker']} gap={c['gap_pct']*100:+.2f}%"
            f" band={c.get('gap_band')}"
            f" price={c['expected_price']:,.0f}"
            f" qty={c.get('expected_qty',0):,}"
            f" amount={c.get('expected_amount',0)/1e8:.1f}억"
        )

    h.ok("f1_filter", f"raw={len(raw)}, gap={len(gap_filtered)}, pass={len(candidates)}")
    return True, candidates


if __name__ == "__main__":
    ok, _ = asyncio.run(run())
    raise SystemExit(0 if ok else 1)
