import json
from pathlib import Path

import pytest

import scripts.kis_phase5_historical_poc as poc
from scripts.kis_phase5_historical_poc import (
    analyze_snapshots,
    compare_kis_daily,
    parse_snapshot_time,
    select_representative_snapshots,
)


def _write_snapshot(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _row(ticker: str, gap: float, *, allowed: bool = True) -> dict:
    previous = 10_000
    return {
        "ticker": ticker,
        "expected_price": previous * (1 + gap),
        "prev_close": previous,
        "gap_pct": gap,
        "expected_amount": 1_000_000,
        "vi_gap": 0.2,
        "gap_allowed": allowed,
        "gap_band": "CORE_GAP" if gap < 0.08 else "HIGH_GAP",
    }


def test_parse_snapshot_time_assigns_kst() -> None:
    parsed = parse_snapshot_time(Path("20260722_090108.jsonl"))

    assert parsed is not None
    assert parsed.isoformat() == "2026-07-22T09:01:08+09:00"
    assert parse_snapshot_time(Path("invalid.jsonl")) is None


def test_select_representative_filters_window_closed_days_and_duplicates(tmp_path: Path) -> None:
    names = [
        "20260720_090108.jsonl",
        "20260720_090303.jsonl",
        "20260721_090108.jsonl",
        "20260722_102000.jsonl",
        "20260725_090108.jsonl",
    ]
    paths = [tmp_path / name for name in names]

    selected, inventory = select_representative_snapshots(paths, {"20260721"})

    assert [path.name for path in selected] == ["20260720_090108.jsonl"]
    assert inventory["duplicate_eligible_count"] == 1
    assert inventory["known_market_closed_count"] == 1
    assert inventory["outside_capture_window_count"] == 1
    assert inventory["weekend_count"] == 1
    assert inventory["timezone"] == "Asia/Seoul"


def test_analyze_snapshots_uses_ratio_gap_bands_and_checks_formula(tmp_path: Path) -> None:
    path = tmp_path / "20260722_090108.jsonl"
    bad_formula = _row("000003", 0.05)
    bad_formula["expected_price"] = 12_000
    missing = _row("000004", 0.02)
    missing["vi_gap"] = None
    _write_snapshot(
        path,
        [_row("000001", 0.03), _row("000002", 0.08), bad_formula, missing],
    )

    quality, rows = analyze_snapshots([path])

    assert quality["row_count"] == 4
    assert quality["core_3_to_8_count"] == 2
    assert quality["conditional_8_to_10_count"] == 1
    assert quality["outside_3_to_10_count"] == 1
    assert quality["gap_formula_mismatch_count"] == 1
    assert quality["missing_required_row_count"] == 1
    assert quality["missing_fields"] == {"vi_gap": 1}
    assert ("20260722", "000001") in rows


@pytest.mark.asyncio
async def test_kis_comparison_stops_on_first_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    async def fake_auth() -> bool:
        return True

    async def fake_get(*args, **kwargs) -> dict:
        nonlocal calls
        calls += 1
        return {"rt_cd": "1", "msg_cd": "EGW00201", "msg1": "rate limit"}

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setattr(poc, "_assert_safe_live_window", lambda now: None)
    monkeypatch.setattr(poc.auth, "load_or_refresh", fake_auth)
    monkeypatch.setattr(poc.kis_rest, "get", fake_get)
    samples = [
        ("20260722", "000001", _row("000001", 0.03)),
        ("20260721", "000002", _row("000002", 0.04)),
    ]

    result = await compare_kis_daily(samples)

    assert result["status"] == "STOPPED"
    assert result["stop_reason"] == "RATE_LIMIT"
    assert result["msg_cd"] == "EGW00201"
    assert calls == 1
