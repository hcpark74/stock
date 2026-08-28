"""전 세션 분봉 백필 검증.

백필이 봉을 중복시키거나 커서를 헛돌면 표본이 조용히 망가진다. 그리고 이
스크립트는 KIS를 1,300번 때리므로 시간 가드가 틀리면 장중에 A의 유량을 먹는다.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from scripts.fast_path_counterfactual import PocStop, Throttle
from scripts.track_b_backfill import (
    assert_backfill_window,
    merge_bars,
    next_cursor,
)

KST = ZoneInfo("Asia/Seoul")


def _bar(time_: str, close: float = 100.0) -> dict:
    return {
        "date": "20260820",
        "time": time_,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 10.0,
    }


def test_merge_dedupes_and_sorts():
    existing = [_bar("091000"), _bar("090000")]
    fetched = [_bar("090000", close=999.0), _bar("085900")]
    merged = merge_bars(existing, fetched)
    assert [b["time"] for b in merged] == ["085900", "090000", "091000"]
    # 기존 값을 유지한다. 같은 봉을 다시 받아도 캐시가 흔들리면 안 된다.
    assert merged[1]["close"] == 100.0


def test_next_cursor_is_earliest_bar():
    assert next_cursor([_bar("091000"), _bar("090500")]) == "090500"


def test_next_cursor_none_when_empty():
    assert next_cursor([]) is None


def test_window_rejects_before_1540():
    with pytest.raises(PocStop) as exc:
        assert_backfill_window(datetime(2026, 8, 28, 15, 39, 59, tzinfo=KST))
    assert exc.value.reason == "AFTER_1540_ONLY"


def test_window_allows_after_1540():
    assert_backfill_window(datetime(2026, 8, 28, 15, 40, tzinfo=KST)) is None


from unittest.mock import AsyncMock, patch

from scripts.track_b_backfill import fetch_session_bars
from src.api import kis_rest


def _response(times: list[str]) -> dict:
    return {
        "rt_cd": "0",
        "output2": [
            {
                "stck_bsop_date": "20260820",
                "stck_cntg_hour": t,
                "stck_oprc": "100",
                "stck_hgpr": "101",
                "stck_lwpr": "99",
                "stck_prpr": "100",
                "cntg_vol": "10",
            }
            for t in times
        ],
    }


async def test_fetch_session_pages_backwards_until_session_start():
    pages = [
        _response(["093000", "092900"]),
        _response(["092800", "090000"]),
    ]
    calls: list[str] = []

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        calls.append(hour_cursor)
        return pages[len(calls) - 1]

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    # 첫 커서는 장 마감, 그다음은 직전 페이지의 가장 이른 봉이다.
    assert calls == ["153000", "092900"]
    # 09:00에 닿으면 멈춘다 — 더 밀면 전일 봉이 섞인다.
    assert [b["time"] for b in bars] == ["090000", "092800", "092900", "093000"]


async def test_fetch_session_drops_other_dates():
    page = _response(["090000"])
    page["output2"].append({
        "stck_bsop_date": "20260819",
        "stck_cntg_hour": "151900",
        "stck_oprc": "1", "stck_hgpr": "1", "stck_lwpr": "1",
        "stck_prpr": "1", "cntg_vol": "1",
    })

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        return page

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    assert {b["date"] for b in bars} == {"20260820"}


async def test_fetch_session_stops_when_cursor_stalls():
    """같은 페이지가 계속 오면 멈춘다. 안 멈추면 예산을 다 태운다."""
    calls = {"n": 0}

    async def fake_fetch(ticker, trade_date, *, budget, hour_cursor="093000"):
        calls["n"] += 1
        return _response(["093000"])

    with patch("scripts.track_b_backfill.fetch_daily_minute_bars", fake_fetch):
        bars = await fetch_session_bars(
            "005930", "20260820",
            budget=kis_rest.CallBudget(10),
            throttle=Throttle(0.0),
        )

    assert calls["n"] == 2
    assert [b["time"] for b in bars] == ["093000"]


import json

from scripts.track_b_backfill import is_session_complete, needed_pairs


def test_needed_pairs_uses_operational_ranking(tmp_path):
    # f1_selector 의 바닥 조건: gap_pct 는 [0.025, 0.100), expected_amount 는
    # 1억 이상. expected_amount 는 expected_price×volume 이 아니라 그 이름의
    # 필드(없으면 avg_amount_5d)를 읽는다.
    rows = [
        {"ticker": "000001", "gap_pct": 0.05, "prev_close": 950,
         "expected_amount": 5_000_000_000, "avg_amount_5d": 1_000_000_000},
        {"ticker": "000002", "gap_pct": 0.04, "prev_close": 960,
         "expected_amount": 3_000_000_000, "avg_amount_5d": 1_000_000_000},
    ]
    # load_universes 는 MIN_UNIVERSE_ROWS(30) 미만을 버린다. 같은 행을 늘려 채운다.
    padded = []
    for i in range(30):
        row = dict(rows[i % 2])
        row["ticker"] = f"{i:06d}"
        padded.append(row)
    path = tmp_path / "20260820_090100.jsonl"
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in padded), encoding="utf-8"
    )

    needed = needed_pairs(depth=5, snapshot_dir=tmp_path)
    assert set(needed) == {"20260820"}
    assert len(needed["20260820"]) <= 5


def test_session_complete_skips_full_days_only():
    assert is_session_complete([{"time": "090000"}] * 380) is True
    assert is_session_complete([{"time": "090000"}] * 31) is False
    assert is_session_complete(None) is False
