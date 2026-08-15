"""당일 분봉 읽기 전용 PoC 순수 함수 + CallBudget 회계 테스트 (전부 mock)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

import scripts.kis_minute_bar_poc as poc
from src.api import kis_rest

KST = ZoneInfo("Asia/Seoul")


def _bar(hhmmss, o=100, h=110, low=90, c=105, v=100, date="20260813"):
    return {
        "stck_bsop_date": date,
        "stck_cntg_hour": hhmmss,
        "stck_oprc": str(o),
        "stck_hgpr": str(h),
        "stck_lwpr": str(low),
        "stck_prpr": str(c),
        "cntg_vol": str(v),
    }


# ── 순수 함수 ───────────────────────────────────────────────────────────

def test_parse_minute_bars_sorts_and_counts_issues():
    resp = {
        "output2": [
            _bar("090200", 100, 110, 95, 105),
            _bar("090100", 100, 108, 98, 101),
            {"stck_bsop_date": "20260813", "stck_cntg_hour": "090300"},  # 필드 누락
            {"stck_cntg_hour": "090400"},  # 시각만
        ]
    }
    bars, issues = poc.parse_minute_bars(resp)
    assert [b["time"] for b in bars] == ["090100", "090200"]
    assert issues["field_missing"] == 2


def test_parse_minute_bars_missing_container_raises():
    with pytest.raises(poc.PocStop):
        poc.parse_minute_bars({"rt_cd": "0"})


def test_mfe_mae_window():
    bars = [
        {"time": "090100", "high": 110, "low": 99, "close": 105},
        {"time": "092900", "high": 130, "low": 90, "close": 120},
        {"time": "093100", "high": 200, "low": 50, "close": 150},  # 창 밖
    ]
    r = poc.mfe_mae(bars, 100.0, start="0900", end="0930")
    assert r["mfe_pct"] == pytest.approx(30.0)
    assert r["mae_pct"] == pytest.approx(-10.0)
    assert r["mfe_time"] == "092900"
    assert r["mae_time"] == "092900"


def test_mfe_mae_no_bars_in_window():
    assert poc.mfe_mae([{"time": "093100", "high": 1, "low": 1, "close": 1}], 100.0) is None


@pytest.mark.parametrize(
    "high,low,expected",
    [
        (130, 99, "UP_FIRST"),
        (101, 80, "DOWN_FIRST"),
        (130, 80, "AMBIGUOUS"),
        (101, 99, "NONE"),
    ],
)
def test_barrier_order(high, low, expected):
    bar = {"high": high, "low": low}
    assert poc.barrier_order(bar, up=120, down=90) == expected


def test_ambiguous_ratio():
    bars = [
        {"high": 130, "low": 80},   # AMBIGUOUS
        {"high": 130, "low": 99},   # UP_FIRST
        {"high": 101, "low": 99},   # NONE (untouched)
    ]
    r = poc.ambiguous_ratio(bars, up=120, down=90)
    assert r["ambiguous"] == 1
    assert r["touched"] == 2
    assert r["ratio"] == pytest.approx(0.5)


# ── 안전 게이트 ─────────────────────────────────────────────────────────

def test_live_window_blocks_open_auction():
    with pytest.raises(poc.PocStop) as e:
        poc._assert_safe_live_window(datetime(2026, 8, 13, 9, 5, tzinfo=KST))
    assert e.value.reason == "FORBIDDEN_0900_0911"


def test_live_window_requires_after_0935():
    with pytest.raises(poc.PocStop) as e:
        poc._assert_safe_live_window(datetime(2026, 8, 13, 9, 20, tzinfo=KST))
    assert e.value.reason == "AFTER_0935_ONLY"


def test_live_window_allows_intraday_after_0935():
    poc._assert_safe_live_window(datetime(2026, 8, 13, 9, 40, tzinfo=KST))
    poc._assert_safe_live_window(datetime(2026, 8, 13, 15, 45, tzinfo=KST))


# ── CallBudget: 실제 HTTP 시도(내부 재시도 포함) 정확 카운트 ─────────────

@pytest.mark.asyncio
async def test_budget_counts_each_attempt_and_rejects_over_budget(monkeypatch):
    class FakeClient:
        async def request(self, *a, **k):
            raise httpx.ConnectError("boom")  # GET에서 재시도 가능한 오류

    async def fake_get_client():
        return FakeClient()

    async def no_wait(_priority):
        return None

    monkeypatch.setattr(kis_rest, "_get_client", fake_get_client)
    monkeypatch.setattr(kis_rest, "_wait_for_rate_slot", no_wait)
    monkeypatch.setattr(kis_rest, "_transient_sleep_seconds", lambda r: 0.0)
    # 재시도 상한을 넉넉히 고정해 예산 초과 지점이 결정론적이게 한다.
    monkeypatch.setattr(kis_rest, "_MAX_TRANSIENT_RETRIES", 5)
    monkeypatch.setenv("KIS_BASE_URL", "https://example.test")

    budget = kis_rest.CallBudget(2)
    with pytest.raises(kis_rest.RequestBudgetExceeded):
        await kis_rest.get("/x", tr_id="T", budget=budget)
    # 시도1·시도2는 실제 전송 후 ConnectError, 시도3은 전송 전 예산 거절.
    assert budget.used == 2


def test_select_target_picks_latest_date_first_valid_ticker(tmp_path):
    import argparse
    import json as _json

    from src.modules import f1_snapshot_selector as f1_sel

    def _snap(path, rows):
        path.write_text(
            "\n".join(_json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        f1_sel.write_completion_sidecar(path)

    row = {
        "ticker": "111111", "expected_price": 12000, "prev_close": 11000,
        "gap_pct": 0.03, "expected_amount": 2_000_000_000, "vi_gap": 0.02,
        "gap_allowed": True, "gap_band": "core",
    }
    older = tmp_path / "20260811_090000.jsonl"
    latest = tmp_path / "20260813_090000.jsonl"
    _snap(older, [dict(row, ticker="999999")])
    _snap(latest, [dict(row, ticker="222222"), dict(row, ticker="333333")])

    args = argparse.Namespace(
        snapshot_dir=str(tmp_path),
        db_path=str(tmp_path / "nope.db"),
        log_dir=None,
        min_coverage=1,
    )
    target = poc.select_target(args)
    assert target["trade_date"] == "20260813"
    assert target["ticker"] == "222222"  # 최신 날짜의 첫 유효 종목
    assert target["ref_price"] == 12000.0


def test_analyze_reports_mfe_mae_and_ambiguous_with_approved_barriers():
    ref = 10_000.0
    bars = [
        # 090100: 상·하 장벽 동시 도달 → AMBIGUOUS
        {"date": "20260813", "time": "090100", "high": 10_260, "low": 9_790, "close": 10_100},
        # 092900: 상승 장벽만 도달 → UP_FIRST
        {"date": "20260813", "time": "092900", "high": 10_300, "low": 9_990, "close": 10_150},
        # 093100: 창 밖(무시)
        {"date": "20260813", "time": "093100", "high": 99_999, "low": 1, "close": 5},
    ]
    a = poc.analyze_minute_bars(bars, ref)
    assert a["up_barrier"] == pytest.approx(10_250.0)   # +2.5%
    assert a["down_barrier"] == pytest.approx(9_800.0)  # -2.0%
    assert a["window_bar_count"] == 2
    assert a["ambiguous"]["ambiguous"] == 1
    assert a["ambiguous"]["touched"] == 2
    assert a["ambiguous"]["ratio"] == pytest.approx(0.5)
    assert a["mfe_mae_0900_0930"]["mfe_time"] == "092900"


@pytest.mark.asyncio
async def test_fetch_paginates_and_stops_on_no_progress(monkeypatch):
    # 커서별 페이지: 두 페이지 후 진전 없음 → 중단.
    pages = {
        "": {"rt_cd": "0", "output2": [_bar("090300"), _bar("090200")]},
        "090200": {"rt_cd": "0", "output2": [_bar("090200"), _bar("090100")]},
        "090100": {"rt_cd": "0", "output2": [_bar("090100")]},  # 새 봉 없음 → 중단
    }
    calls = []

    async def fake_fetch(ticker, *, budget, hour_cursor=""):
        calls.append(hour_cursor)
        budget.charge()
        return pages[hour_cursor]

    monkeypatch.setattr(poc, "fetch_minute_bars", fake_fetch)
    budget = poc.kis_rest.CallBudget(60)
    bars, issues = await poc.fetch_all_minute_bars(
        "005930", "20260813", budget=budget, max_pages=10
    )
    times = [b["time"] for b in bars]
    assert times == ["090100", "090200", "090300"]  # 정렬·중복 제거
    assert calls == ["", "090200", "090100"]
    assert issues["pages_fetched"] == 3


@pytest.mark.asyncio
async def test_fetch_filters_other_days(monkeypatch):
    async def fake_fetch(ticker, *, budget, hour_cursor=""):
        budget.charge()
        return {"rt_cd": "0", "output2": [_bar("090100"), _bar("090100", date="20260812")]}

    monkeypatch.setattr(poc, "fetch_minute_bars", fake_fetch)
    budget = poc.kis_rest.CallBudget(60)
    bars, _ = await poc.fetch_all_minute_bars(
        "005930", "20260813", budget=budget, max_pages=3
    )
    assert all(b["date"] == "20260813" for b in bars)


@pytest.mark.asyncio
async def test_budget_counts_single_successful_call(monkeypatch):
    class OkResp:
        status_code = 200
        headers: dict = {}

        def json(self):
            return {"rt_cd": "0", "output2": []}

    class FakeClient:
        async def request(self, *a, **k):
            return OkResp()

    async def fake_get_client():
        return FakeClient()

    async def no_wait(_priority):
        return None

    monkeypatch.setattr(kis_rest, "_get_client", fake_get_client)
    monkeypatch.setattr(kis_rest, "_wait_for_rate_slot", no_wait)
    monkeypatch.setenv("KIS_BASE_URL", "https://example.test")

    budget = kis_rest.CallBudget(60)
    await kis_rest.get("/x", tr_id="T", budget=budget)
    assert budget.used == 1
