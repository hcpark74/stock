import json

import pytest
from fastapi.testclient import TestClient

from src import bars
from src.api.server import app
from src.modules import tick_capture

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_bars(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def _row(minute, close, *, confirmed=True, derived=True):
    return {
        "date": "20260827", "time": f"09{minute:02d}00",
        "open": close - 5, "high": close + 10, "low": close - 10, "close": close,
        "volume": 100.0, "confirmed": confirmed,
        "tick_count": 12, "spike_dropped": 1 if minute == 0 else 0,
        "tick_derived": ({"cttr": 120.0, "askp1": None, "bidp1": None,
                          "total_askp_rsqn": None, "total_bidp_rsqn": None,
                          "vol_by_ccld": {}, "corrected": False} if derived else None),
    }


def test_bars_endpoint_returns_bars_indicators_and_meta(tmp_path):
    rows = [_row(m, 14500 + m * 10) for m in range(30)]
    (tmp_path / "20260827_006340.json").write_text(json.dumps(rows), encoding="utf-8")

    res = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"})
    body = res.json()

    assert res.status_code == 200
    assert body["ticker"] == "006340"
    assert body["track"] == "B"
    assert len(body["bars"]) == 30
    assert len(body["indicators"]["sma"]) == 30
    assert len(body["indicators"]["macd"]) == 30
    assert body["meta"]["bar_count"] == 30
    assert body["meta"]["source"] == "file"


def test_indicator_arrays_align_with_the_bar_array():
    for m in range(30):
        bars._series.setdefault(("20260827", "006340"), {})[f"09{m:02d}00"] = _row(m, 14500 + m * 10)

    body = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"}).json()

    assert body["indicators"]["sma"][:19] == [None] * 19
    assert body["indicators"]["sma"][19] is not None
    assert body["meta"]["source"] == "memory"


def test_meta_counts_unconfirmed_bars_and_missing_tick_derived():
    minutes = bars._series.setdefault(("20260827", "006340"), {})
    minutes["090000"] = _row(0, 14500, confirmed=True)
    minutes["090100"] = _row(1, 14510, confirmed=False)
    minutes["090200"] = _row(2, 14520, confirmed=True, derived=False)

    body = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"}).json()

    assert body["meta"]["bar_count"] == 3
    assert body["meta"]["confirmed_count"] == 2
    assert body["meta"]["tick_derived_missing"] == 1
    assert body["meta"]["spike_dropped"] == 1


def test_unknown_series_returns_empty_arrays_not_an_error():
    res = client.get("/api/bars", params={"date": "20991231", "ticker": "000000"})
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["indicators"]["sma"] == []
    assert body["meta"]["bar_count"] == 0


def test_indicator_periods_are_configurable():
    for m in range(10):
        bars._series.setdefault(("20260827", "006340"), {})[f"09{m:02d}00"] = _row(m, 14500 + m * 10)

    body = client.get(
        "/api/bars",
        params={"date": "20260827", "ticker": "006340", "sma": 3, "fast": 2, "slow": 4, "signal": 2},
    ).json()

    assert body["indicators"]["sma"][2] is not None
    assert body["indicators"]["macd"][3]["macd"] is not None


def test_ticker_path_traversal_is_rejected_and_contained(tmp_path):
    # tmp_path here is the same instance the autouse isolated_bars fixture patched
    # bars._BARS_DIR to. Plant a marker file OUTSIDE that directory and try to
    # reach it with a ticker containing ".." segments.
    outside_marker = tmp_path.parent / "marker_outside.json"
    outside_marker.write_text(json.dumps([{"secret": "outside-data"}]), encoding="utf-8")

    res = client.get(
        "/api/bars",
        params={"date": "20260827", "ticker": "x/../../marker_outside"},
    )
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["meta"]["source"] == "invalid"
    assert "outside-data" not in res.text


def test_malformed_bars_file_is_reported_as_empty_not_memory(tmp_path):
    (tmp_path / "20260827_006340.json").write_text("{not valid json", encoding="utf-8")

    res = client.get("/api/bars", params={"date": "20260827", "ticker": "006340"})
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["indicators"]["sma"] == []
    assert body["meta"]["source"] == "empty"


def test_bad_date_format_is_rejected_without_an_error():
    res = client.get("/api/bars", params={"date": "2026-08-27", "ticker": "006340"})
    body = res.json()

    assert res.status_code == 200
    assert body["bars"] == []
    assert body["indicators"]["sma"] == []
    assert body["meta"]["source"] == "invalid"
