from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.api import kis_minute_bars as mb

KST = ZoneInfo("Asia/Seoul")


def _row(time_, o, h, low, c, v):
    return {
        "stck_bsop_date": "20260827",
        "stck_cntg_hour": time_,
        "stck_oprc": str(o),
        "stck_hgpr": str(h),
        "stck_lwpr": str(low),
        "stck_prpr": str(c),
        "cntg_vol": str(v),
    }


def test_parse_sorts_bars_by_date_and_time():
    resp = {"output2": [_row("093500", 1, 2, 1, 2, 10), _row("093400", 1, 2, 1, 1, 5)]}

    bars, issues = mb.parse_minute_bars(resp)

    assert [b["time"] for b in bars] == ["093400", "093500"]
    assert issues == {"empty_bar": 0, "field_missing": 0}


def test_parse_counts_rows_with_missing_fields_and_excludes_them():
    resp = {"output2": [_row("093500", 1, 2, 1, 2, 10), {"stck_cntg_hour": "093600"}, {}]}

    bars, issues = mb.parse_minute_bars(resp)

    assert len(bars) == 1
    assert issues["field_missing"] == 1
    assert issues["empty_bar"] == 1


def test_parse_falls_back_to_output_key():
    bars, _ = mb.parse_minute_bars({"output": [_row("093500", 1, 2, 1, 2, 10)]})

    assert len(bars) == 1


def test_parse_raises_when_no_row_container_exists():
    with pytest.raises(mb.MinuteBarError):
        mb.parse_minute_bars({"rt_cd": "0"})


def test_forbidden_window_covers_0900_to_0911():
    assert mb.in_forbidden_window(datetime(2026, 8, 27, 9, 0, 0, tzinfo=KST))
    assert mb.in_forbidden_window(datetime(2026, 8, 27, 9, 10, 59, tzinfo=KST))
    assert not mb.in_forbidden_window(datetime(2026, 8, 27, 9, 11, 0, tzinfo=KST))
    assert not mb.in_forbidden_window(datetime(2026, 8, 27, 8, 59, 59, tzinfo=KST))


async def test_fetch_uses_background_priority_and_stops_on_rate_limit(monkeypatch):
    seen = {}

    async def fake_get(path, **kwargs):
        seen["path"] = path
        seen.update(kwargs)
        return {"rt_cd": "0", "output2": []}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    await mb.fetch_minute_bars("006340")

    assert seen["path"] == mb.MINUTE_PATH
    assert seen["tr_id"] == mb.MINUTE_TR
    assert seen["request_priority"] == mb.kis_rest.REQUEST_PRIORITY_BACKGROUND
    assert seen["stop_on_rate_limit"] is True
    assert seen["params"]["FID_INPUT_ISCD"] == "006340"


async def test_fetch_raises_on_nonzero_rt_cd(monkeypatch):
    async def fake_get(path, **kwargs):
        return {"rt_cd": "1", "msg_cd": "EGW00123"}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    with pytest.raises(mb.MinuteBarError):
        await mb.fetch_minute_bars("006340")


async def test_fetch_day_bars_stops_when_cursor_makes_no_progress(monkeypatch):
    calls = {"n": 0}

    async def fake_get(path, **kwargs):
        calls["n"] += 1
        return {"rt_cd": "0", "output2": [_row("093500", 1, 2, 1, 2, 10)]}

    monkeypatch.setattr(mb.kis_rest, "get", fake_get)

    bars, issues = await mb.fetch_day_bars("006340", max_pages=10)

    assert len(bars) == 1          # 중복 제거
    assert calls["n"] == 2         # 두 번째 페이지에서 새 봉 0 → 중단
