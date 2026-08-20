"""Fast Path 반사실 평가 순수 함수 테스트 (외부 호출 없음)."""
import json
from datetime import datetime

import pytest

import scripts.fast_path_counterfactual as cf
from src.api import kis_rest


def _budget(max_calls=60):
    return kis_rest.CallBudget(max_calls)


def _raw_bar(hhmmss, o=100, h=110, low=90, c=105, v=100, date="20260819"):
    """KIS 분봉 응답 원시 행."""
    return {
        "stck_bsop_date": date,
        "stck_cntg_hour": hhmmss,
        "stck_oprc": str(o),
        "stck_hgpr": str(h),
        "stck_lwpr": str(low),
        "stck_prpr": str(c),
        "cntg_vol": str(v),
    }


def _open_multi(output, phase="OPEN", market="SHORTLIST"):
    return {
        "ts": "2026-08-19T09:00:00.441255+09:00",
        "event": "PAPER_FAST_PROBE_OPEN_MULTI",
        "phase": phase,
        "market": market,
        "response": {"rt_cd": "0", "output": output},
    }


def _quote(ticker, askp, prpr="0"):
    return {"inter_shrn_iscd": ticker, "inter2_askp": str(askp), "inter2_prpr": prpr}


def _open_done(shadow_tickers, ok=True):
    return {
        "ts": "2026-08-19T09:00:00.442254+09:00",
        "event": "PAPER_FAST_PROBE_OPEN_DONE",
        "shadow_tickers": list(shadow_tickers),
        "quality": {"ok": ok, "reason": "COMPLETE" if ok else "INCOMPLETE"},
    }


def _compare(legacy_tickers):
    return {
        "ts": "2026-08-19T09:01:29.388973+09:00",
        "event": "PAPER_FAST_SHADOW_COMPARE",
        "legacy_tickers": list(legacy_tickers),
    }


def _bar(hhmmss, o, h, low, c, date="20260819"):
    return {"date": date, "time": hhmmss, "open": o, "high": h, "low": low, "close": c}


# ── 프로브 파일 파싱 ─────────────────────────────────────────────────────

def test_extract_ask_prices_reads_open_phase_only():
    records = [
        _open_multi([_quote("111111", 999)], phase="PREOPEN", market="J"),
        _open_multi([_quote("126640", 3180), _quote("006660", 15650)]),
    ]
    prices = cf.extract_ask_prices(records)
    assert prices == {"126640": 3180.0, "006660": 15650.0}


def test_extract_ask_prices_skips_non_positive_ask():
    records = [_open_multi([_quote("126640", 0), _quote("006660", 15650)])]
    assert cf.extract_ask_prices(records) == {"006660": 15650.0}


def test_extract_rank1_returns_fast_and_legacy_heads():
    records = [_open_done(["126640", "006660"]), _compare(["006660", "009270"])]
    assert cf.extract_rank1(records) == ("126640", "006660")


def test_extract_rank1_missing_events_yield_none():
    assert cf.extract_rank1([]) == (None, None)


def test_build_day_case_rejects_incomplete_quality():
    records = [
        _open_multi([_quote("126640", 3180)]),
        _open_done(["126640"], ok=False),
        _compare(["006660"]),
    ]
    assert cf.build_day_case("20260819", records) is None


def test_build_day_case_collects_fast_entry_from_probe_ask():
    records = [
        _open_multi([_quote("126640", 3180), _quote("006660", 15650)]),
        _open_done(["126640", "006660"]),
        _compare(["006660", "009270"]),
    ]
    case = cf.build_day_case("20260819", records)
    assert case["date"] == "20260819"
    assert case["fast_ticker"] == "126640"
    assert case["fast_entry"] == pytest.approx(3180.0)
    assert case["legacy_ticker"] == "006660"


def test_build_day_case_allows_legacy_rank1_outside_shortlist():
    """레거시 1순위는 60종목 중에서 나오므로 30종목 shortlist 밖일 수 있다."""
    records = [
        _open_multi([_quote("126640", 3180)]),
        _open_done(["126640"]),
        _compare(["487400"]),
    ]
    case = cf.build_day_case("20260819", records)
    assert case["legacy_ticker"] == "487400"
    assert case["legacy_entry"] is None


def test_read_probe_day_parses_jsonl_and_skips_bad_lines(tmp_path):
    path = tmp_path / "20260819.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_open_multi([_quote("126640", 3180)])),
                "{ not json",
                json.dumps(_open_done(["126640"])),
                json.dumps(_compare(["006660"])),
            ]
        ),
        encoding="utf-8",
    )
    records = cf.read_probe_day(path)
    assert [r["event"] for r in records] == [
        "PAPER_FAST_PROBE_OPEN_MULTI",
        "PAPER_FAST_PROBE_OPEN_DONE",
        "PAPER_FAST_SHADOW_COMPARE",
    ]


# ── 진입가 / 장벽 ────────────────────────────────────────────────────────

def test_entry_price_from_bars_uses_named_bar_open():
    bars = [_bar("090000", 100, 105, 99, 104), _bar("090100", 104, 108, 103, 107)]
    assert cf.entry_price_from_bars(bars, "0900", field="open") == pytest.approx(100.0)


def test_entry_price_from_bars_uses_named_bar_close():
    bars = [_bar("090000", 100, 105, 99, 104), _bar("090100", 104, 108, 103, 107)]
    assert cf.entry_price_from_bars(bars, "0901", field="close") == pytest.approx(107.0)


def test_entry_price_from_bars_returns_none_when_bar_absent():
    bars = [_bar("090500", 100, 105, 99, 104)]
    assert cf.entry_price_from_bars(bars, "0900", field="open") is None


def test_first_barrier_reports_earliest_touch_in_time_order():
    bars = [
        _bar("090000", 100, 101, 99, 100),
        _bar("090100", 100, 102, 99, 102),  # +2.5%(102.5) 미달
        _bar("090200", 102, 100, 97, 98),  # -2.0%(98.0) 도달
    ]
    result = cf.first_barrier(bars, 100.0, start="0900", end="0930")
    assert result["outcome"] == "DOWN_FIRST"
    assert result["time"] == "090200"


def test_first_barrier_flags_same_bar_double_touch_as_ambiguous():
    bars = [_bar("090100", 100, 103, 97, 99)]
    result = cf.first_barrier(bars, 100.0, start="0900", end="0930")
    assert result["outcome"] == "AMBIGUOUS"


def test_first_barrier_returns_none_when_untouched():
    bars = [_bar("090100", 100, 101, 99.5, 100)]
    assert cf.first_barrier(bars, 100.0, start="0900", end="0930")["outcome"] == "NONE"


def test_first_barrier_honors_window_start():
    """레거시 진입은 09:01이므로 09:00 봉의 접촉을 세면 안 된다."""
    bars = [
        _bar("090000", 100, 110, 90, 100),  # 창 밖
        _bar("090100", 100, 101, 99.5, 100),
    ]
    assert cf.first_barrier(bars, 100.0, start="0901", end="0930")["outcome"] == "NONE"


# ── 판정 ────────────────────────────────────────────────────────────────

def test_verdict_prefers_up_first_over_down_first():
    fast = {"outcome": "UP_FIRST"}
    legacy = {"outcome": "DOWN_FIRST"}
    assert cf.verdict(fast, legacy) == "FAST_BETTER"


def test_verdict_marks_legacy_better_when_roles_reversed():
    assert cf.verdict({"outcome": "DOWN_FIRST"}, {"outcome": "UP_FIRST"}) == "LEGACY_BETTER"


def test_verdict_ties_on_identical_outcomes():
    assert cf.verdict({"outcome": "NONE"}, {"outcome": "NONE"}) == "TIE"


def test_verdict_is_undecidable_when_either_side_ambiguous():
    assert cf.verdict({"outcome": "AMBIGUOUS"}, {"outcome": "UP_FIRST"}) == "UNDECIDABLE"


def test_verdict_is_undecidable_when_a_side_is_missing():
    assert cf.verdict(None, {"outcome": "UP_FIRST"}) == "UNDECIDABLE"


def test_verdict_ranks_none_above_down_first():
    """손절 미도달은 손절 도달보다 낫다."""
    assert cf.verdict({"outcome": "NONE"}, {"outcome": "DOWN_FIRST"}) == "FAST_BETTER"


# ── 집계 ────────────────────────────────────────────────────────────────

def test_summarize_counts_verdicts_and_days():
    rows = [
        {"verdict": "FAST_BETTER"},
        {"verdict": "FAST_BETTER"},
        {"verdict": "LEGACY_BETTER"},
        {"verdict": "UNDECIDABLE"},
        {"verdict": "TIE"},
    ]
    summary = cf.summarize(rows)
    assert summary["evaluated_days"] == 5
    assert summary["fast_better"] == 2
    assert summary["legacy_better"] == 1
    assert summary["undecidable"] == 1
    assert summary["tie"] == 1


def test_summarize_reports_decisive_days_excluding_undecidable():
    rows = [{"verdict": "FAST_BETTER"}, {"verdict": "UNDECIDABLE"}]
    assert cf.summarize(rows)["decisive_days"] == 1


def test_summarize_handles_empty_input():
    summary = cf.summarize([])
    assert summary["evaluated_days"] == 0
    assert summary["decisive_days"] == 0


# ── 안전 게이트 ─────────────────────────────────────────────────────────

def test_assert_paper_mode_blocks_real(monkeypatch):
    monkeypatch.setenv("KIS_MODE", "REAL")
    with pytest.raises(cf.PocStop) as exc:
        cf._assert_paper_mode()
    assert exc.value.reason == "PAPER_ONLY"


def test_assert_paper_mode_allows_paper(monkeypatch):
    monkeypatch.setenv("KIS_MODE", "PAPER")
    cf._assert_paper_mode()


def test_assert_safe_live_window_blocks_entry_pipeline_window():
    now = datetime(2026, 8, 19, 9, 5, tzinfo=cf.KST)
    with pytest.raises(cf.PocStop) as exc:
        cf._assert_safe_live_window(now)
    assert exc.value.reason == "FORBIDDEN_0900_0911"


def test_assert_safe_live_window_blocks_before_0935():
    now = datetime(2026, 8, 19, 9, 20, tzinfo=cf.KST)
    with pytest.raises(cf.PocStop) as exc:
        cf._assert_safe_live_window(now)
    assert exc.value.reason == "AFTER_0935_ONLY"


def test_assert_safe_live_window_allows_after_0935():
    cf._assert_safe_live_window(datetime(2026, 8, 19, 15, 40, tzinfo=cf.KST))


# ── 케이스 수집 ─────────────────────────────────────────────────────────

def _write_probe(tmp_path, date, fast, legacy, ask):
    path = tmp_path / f"{date}.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(_open_multi([_quote(fast, ask)])),
                json.dumps(_open_done([fast])),
                json.dumps(_compare([legacy])),
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_collect_cases_reads_every_probe_day_in_date_order(tmp_path):
    _write_probe(tmp_path, "20260818", "010170", "019210", 5000)
    _write_probe(tmp_path, "20260819", "126640", "006660", 3180)
    cases = cf.collect_cases(tmp_path)
    assert [c["date"] for c in cases] == ["20260818", "20260819"]


def test_collect_cases_skips_days_without_usable_case(tmp_path):
    path = tmp_path / "20260817.jsonl"
    path.write_text(json.dumps(_open_done(["126640"], ok=False)), encoding="utf-8")
    _write_probe(tmp_path, "20260819", "126640", "006660", 3180)
    assert [c["date"] for c in cf.collect_cases(tmp_path)] == ["20260819"]


def test_collect_cases_on_missing_directory_returns_empty(tmp_path):
    assert cf.collect_cases(tmp_path / "nope") == []


# ── 한 종목 평가 ────────────────────────────────────────────────────────

def test_evaluate_side_uses_supplied_entry_price():
    bars = [_bar("090000", 100, 105, 99, 104), _bar("090100", 104, 108, 103, 107)]
    result = cf.evaluate_side(bars, entry_price=100.0, entry_bar="0900", entry_field="open")
    assert result["entry_price"] == pytest.approx(100.0)
    assert result["outcome"] == "UP_FIRST"


def test_evaluate_side_falls_back_to_bar_when_entry_price_missing():
    """레거시 1순위가 shortlist 밖이면 진입가가 없어 분봉으로 채운다."""
    bars = [_bar("090100", 104, 108, 103, 107)]
    result = cf.evaluate_side(bars, entry_price=None, entry_bar="0901", entry_field="close")
    assert result["entry_price"] == pytest.approx(107.0)


def test_evaluate_side_returns_none_without_any_price():
    bars = [_bar("090500", 104, 108, 103, 107)]
    assert cf.evaluate_side(bars, entry_price=None, entry_bar="0901", entry_field="close") is None


def test_evaluate_side_reports_excursion():
    bars = [_bar("090000", 100, 110, 95, 104)]
    result = cf.evaluate_side(bars, entry_price=100.0, entry_bar="0900", entry_field="open")
    assert result["mfe_pct"] == pytest.approx(10.0)
    assert result["mae_pct"] == pytest.approx(-5.0)


# ── TR 선택 ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_bars_uses_daily_tr_even_for_today(monkeypatch):
    """당일TR은 빈 커서에서 15:01~15:30을 주므로 오늘자도 일별TR로 읽는다."""
    calls = []

    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="093000"):
        calls.append(trade_date)
        return {"rt_cd": "0", "output2": [_raw_bar("090100", date="20260819")]}

    monkeypatch.setattr(cf, "fetch_daily_minute_bars", fake_daily)
    bars = await cf.load_bars("006660", "20260819", budget=_budget())
    assert calls == ["20260819"]
    assert [b["time"] for b in bars] == ["090100"]


@pytest.mark.asyncio
async def test_load_bars_uses_daily_tr_for_past_date(monkeypatch):
    calls = []

    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="093000"):
        calls.append(trade_date)
        return {"rt_cd": "0", "output2": [_raw_bar("090100", date="20260813")]}

    monkeypatch.setattr(cf, "fetch_daily_minute_bars", fake_daily)
    bars = await cf.load_bars("006660", "20260813", budget=_budget())
    assert calls == ["20260813"]
    assert [b["date"] for b in bars] == ["20260813"]


@pytest.mark.asyncio
async def test_load_bars_raises_on_error_response(monkeypatch):
    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="093000"):
        return {"rt_cd": "1", "msg_cd": "EGW00201"}

    monkeypatch.setattr(cf, "fetch_daily_minute_bars", fake_daily)
    with pytest.raises(cf.PocStop) as exc:
        await cf.load_bars("006660", "20260819", budget=_budget())
    assert exc.value.reason == "RATE_LIMIT"


@pytest.mark.asyncio
async def test_load_bars_keeps_only_requested_trade_date(monkeypatch):
    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="093000"):
        return {
            "rt_cd": "0",
            "output2": [
                _raw_bar("090100", date="20260813"),
                _raw_bar("090100", date="20260812"),
            ],
        }

    monkeypatch.setattr(cf, "fetch_daily_minute_bars", fake_daily)
    bars = await cf.load_bars("006660", "20260813", budget=_budget())
    assert [b["date"] for b in bars] == ["20260813"]


# ── 적시성 (shadow_validation_summary와 동일 규칙) ───────────────────────

def _at(event_maker, ts, *args, **kwargs):
    record = event_maker(*args, **kwargs)
    record["ts"] = ts
    return record


def test_build_day_case_rejects_late_legacy_comparison():
    """개장 3분 뒤의 COMPARE는 수동 재실행이라 개장 선정이 아니다."""
    records = [
        _open_multi([_quote("126640", 3180)]),
        _at(_open_done, "2026-08-06T09:00:00.4+09:00", ["126640"]),
        _at(_compare, "2026-08-06T23:50:00.0+09:00", ["005930"]),
    ]
    assert cf.build_day_case("20260806", records) is None


def test_build_day_case_accepts_comparison_within_three_minutes():
    records = [
        _open_multi([_quote("126640", 3180)]),
        _at(_open_done, "2026-08-19T09:00:00.4+09:00", ["126640"]),
        _at(_compare, "2026-08-19T09:01:29.3+09:00", ["006660"]),
    ]
    assert cf.build_day_case("20260819", records) is not None


def test_build_day_case_rejects_comparison_before_open():
    records = [
        _open_multi([_quote("126640", 3180)]),
        _at(_open_done, "2026-08-19T09:00:00.4+09:00", ["126640"]),
        _at(_compare, "2026-08-19T08:59:00.0+09:00", ["006660"]),
    ]
    assert cf.build_day_case("20260819", records) is None



@pytest.mark.asyncio
async def test_load_bars_drops_substituted_other_date(monkeypatch):
    """휴장일을 요청하면 KIS가 가장 가까운 날로 조용히 대체한다 — 버려야 한다."""

    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="093000"):
        return {"rt_cd": "0", "output2": [_raw_bar("090100", date="20260814")]}

    monkeypatch.setattr(cf, "fetch_daily_minute_bars", fake_daily)
    assert await cf.load_bars("126640", "20260817", budget=_budget()) == []


# ── 빈 창은 미판정 ──────────────────────────────────────────────────────

def test_evaluate_side_returns_none_when_window_has_no_bars():
    """봉이 없으면 '장벽 미접촉'이 아니라 '알 수 없음'이다."""
    assert cf.evaluate_side([], entry_price=100.0, entry_bar="0900", entry_field="open") is None


def test_evaluate_side_returns_none_when_bars_fall_outside_window():
    bars = [_bar("150100", 100, 105, 99, 104)]
    assert (
        cf.evaluate_side(bars, entry_price=100.0, entry_bar="0900", entry_field="open") is None
    )


def test_empty_window_yields_undecidable_not_tie():
    """빈 창 두 개를 TIE로 집계하면 판정력이 사라진다."""
    assert cf.verdict(None, None) == "UNDECIDABLE"


# ── 레거시 진입가는 항상 지연 반영 ──────────────────────────────────────

def test_build_day_case_never_prices_legacy_from_open_ask():
    """레거시는 약 89초 뒤에 진입한다. 09:00 호가를 주면 지연 페널티가 사라진다."""
    records = [
        _open_multi([_quote("126640", 3180), _quote("006660", 15600)]),
        _open_done(["126640", "006660"]),
        _compare(["006660"]),
    ]
    case = cf.build_day_case("20260819", records)
    assert case["fast_entry"] == pytest.approx(3180.0)
    assert case.get("legacy_entry") is None


@pytest.mark.asyncio
async def test_evaluate_case_prices_legacy_at_0901_bar(monkeypatch):
    bars = [
        _raw_bar("090000", o=15620, h=15880, low=15580, c=15730),
        _raw_bar("090100", o=15730, h=16030, low=15410, c=16010),
    ]

    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="093000"):
        return {"rt_cd": "0", "output2": bars}

    monkeypatch.setattr(cf, "fetch_daily_minute_bars", fake_daily)
    case = {
        "date": "20260819",
        "fast_ticker": "126640",
        "fast_entry": 4405.0,
        "legacy_ticker": "006660",
        "legacy_entry": None,
    }
    row = await cf.evaluate_case(case, budget=_budget())
    assert row["legacy"]["entry_price"] == pytest.approx(16010.0)


# ── 호출 페이싱 ─────────────────────────────────────────────────────────

def test_throttle_first_call_waits_nothing():
    throttle = cf.Throttle(interval_sec=1.2)
    assert throttle.wait_seconds(now=100.0) == pytest.approx(0.0)


def test_throttle_waits_remainder_of_interval():
    throttle = cf.Throttle(interval_sec=1.2)
    throttle.mark(now=100.0)
    assert throttle.wait_seconds(now=100.5) == pytest.approx(0.7)


def test_throttle_does_not_wait_once_interval_elapsed():
    throttle = cf.Throttle(interval_sec=1.2)
    throttle.mark(now=100.0)
    assert throttle.wait_seconds(now=101.5) == pytest.approx(0.0)


def test_throttle_never_returns_negative_wait():
    throttle = cf.Throttle(interval_sec=1.2)
    throttle.mark(now=100.0)
    assert throttle.wait_seconds(now=99.0) >= 0.0


@pytest.mark.asyncio
async def test_load_bars_marks_throttle_on_each_call(monkeypatch):
    async def fake_daily(ticker, trade_date, *, budget, hour_cursor="093000"):
        return {"rt_cd": "0", "output2": [_raw_bar("090100", date="20260819")]}

    monkeypatch.setattr(cf, "fetch_daily_minute_bars", fake_daily)
    throttle = cf.Throttle(interval_sec=0.0)
    await cf.load_bars("006660", "20260819", budget=_budget(), throttle=throttle)
    assert throttle.last_call is not None
