import json

import pytest

pytest.importorskip("fastapi")

import src.api.server as server  # noqa: E402 — fastapi 미설치 시 모듈 스킵 이후 임포트
from src import db  # noqa: E402


def _trade(**over):
    base = {
        "date": "20260701", "ticker": "005930", "name": "삼성전자",
        "entry_price": 10_000.0, "high_price": None, "highest_step": 0.0,
        "pnl_pct": 0.0, "close_reason": "TIMEOUT",
        "entry_at": "2026-07-01T09:12:00+09:00",
        "exit_at": "2026-07-01T11:00:00+09:00",
    }
    base.update(over)
    return base


def _order(phase, order_price, fill_price, latency=500, trigger_price=None):
    row = {
        "order_phase": phase, "order_price": order_price,
        "fill_price": fill_price, "fill_latency_ms": latency,
    }
    if trigger_price is not None:
        row["trigger_price"] = trigger_price
    return row


def test_improve_empty_rows_returns_zero_structure():
    payload = server._improve_from_rows([], [], {})

    assert payload["overall"] == {
        "total": 0, "wins": 0, "win_rate": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "payoff_ratio": 0.0, "expectancy": 0.0,
        "cur_loss_streak": 0, "max_loss_streak": 0,
    }
    assert payload["mfe_rows"] == []
    assert payload["hold_time"] == {}
    assert payload["params"]["step_size_pct"] == 2.5
    assert payload["params"]["step_trail_pct"] == 1.5
    assert payload["params"]["hard_stop_pct"] == 2.0
    assert payload["params"]["gap_max_order_pct"] == 6.5
    assert payload["params"]["gap_max_fill_pct"] == 7.0
    assert payload["params"]["timeout_time"] == "11:00"
    assert payload["params"]["force_trailing_time"] == "10:50"


def test_improve_overall_payoff_and_expectancy():
    trades = [
        _trade(date="20260701", pnl_pct=2.0, close_reason="TRAILING"),
        _trade(date="20260702", pnl_pct=-1.0, close_reason="HARD_STOP"),
        _trade(date="20260703", pnl_pct=-1.0, close_reason="HARD_STOP"),
    ]

    o = server._improve_from_rows(trades, [], {})["overall"]

    assert o["total"] == 3
    assert o["wins"] == 1
    assert o["win_rate"] == 33.3
    assert o["avg_win"] == 2.0
    assert o["avg_loss"] == -1.0
    assert o["payoff_ratio"] == 2.0
    # 기대값 = 1/3*2.0 + 2/3*(-1.0) = 0.0
    assert o["expectancy"] == 0.0


def test_improve_loss_streaks_track_current_and_max():
    # 순서: 패 패 승 패 → 최대 2, 현재 1
    trades = [
        _trade(date="20260701", pnl_pct=-1.0),
        _trade(date="20260702", pnl_pct=-1.0),
        _trade(date="20260703", pnl_pct=1.0),
        _trade(date="20260704", pnl_pct=-1.0),
    ]

    o = server._improve_from_rows(trades, [], {})["overall"]

    assert o["max_loss_streak"] == 2
    assert o["cur_loss_streak"] == 1


def test_improve_mfe_rows_newest_first_with_giveback():
    trades = [
        _trade(date="20260701", high_price=10_210.0, pnl_pct=-2.13,
               close_reason="HARD_STOP"),
        _trade(date="20260702", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
    ]

    rows = server._improve_from_rows(trades, [], {})["mfe_rows"]

    assert [r["date"] for r in rows] == ["20260702", "20260701"]
    assert rows[1]["mfe_pct"] == 2.1
    assert rows[1]["giveback_pp"] == 4.23
    assert rows[0]["mfe_pct"] == 4.0
    assert rows[0]["giveback_pp"] == 3.0


def test_improve_mfe_null_when_high_price_missing():
    rows = server._improve_from_rows([_trade(high_price=None)], [], {})["mfe_rows"]

    assert rows[0]["mfe_pct"] is None
    assert rows[0]["giveback_pp"] is None


def test_improve_step_counts_near_miss_and_step1():
    trades = [
        # 근접 이탈: 스텝1 미도달 + MFE 2.1% + 손실
        _trade(date="20260701", high_price=10_210.0, pnl_pct=-2.13,
               close_reason="HARD_STOP"),
        # 스텝1 도달
        _trade(date="20260702", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
        # MFE 1.5% + 손실 → 근접 이탈 (경계값 포함)
        _trade(date="20260703", high_price=10_150.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
        # MFE 1.4% → 근접 이탈 아님
        _trade(date="20260704", high_price=10_140.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
        # MFE 2.0%지만 수익 마감 → 근접 이탈 아님
        _trade(date="20260705", high_price=10_200.0, pnl_pct=0.3,
               close_reason="TIMEOUT"),
    ]

    s = server._improve_from_rows(trades, [], {})["step"]

    assert s["step1_n"] == 1
    assert s["step1_rate"] == 20.0
    assert s["near_miss_n"] == 2


def test_improve_hard_stop_slip_and_fast_stop():
    trades = [
        # -2.13% 체결, 8분 보유 → 편차 0.13%p, 빠른 손절
        _trade(date="20260701", pnl_pct=-2.13, close_reason="HARD_STOP",
               entry_at="2026-07-01T09:12:00+09:00",
               exit_at="2026-07-01T09:20:00+09:00"),
        # -2.07% 체결, 30분 보유
        _trade(date="20260702", pnl_pct=-2.07, close_reason="HARD_STOP",
               entry_at="2026-07-02T09:12:00+09:00",
               exit_at="2026-07-02T09:42:00+09:00"),
    ]

    h = server._improve_from_rows(trades, [], {})["hard_stop"]

    assert h["n"] == 2
    assert h["share_pct"] == 100.0
    assert h["avg_fill_pnl"] == -2.1
    assert h["avg_slip_pp"] == 0.1
    assert h["fast_stop_n"] == 1
    assert h["avg_min_to_stop"] == 19.0


def test_improve_trailing_and_timeout_sections():
    trades = [
        _trade(date="20260701", high_price=10_400.0, pnl_pct=1.0,
               close_reason="TRAILING", highest_step=0.025),
        _trade(date="20260702", high_price=10_150.0, pnl_pct=-0.5,
               close_reason="TIMEOUT"),
    ]

    payload = server._improve_from_rows(trades, [], {})

    assert payload["trailing"] == {"n": 1, "avg_giveback_pp": 3.0, "avg_pnl": 1.0}
    assert payload["timeout_exit"] == {"n": 1, "avg_pnl": -0.5, "avg_mfe": 1.5}


def test_improve_hold_time_grouped_by_reason():
    trades = [
        _trade(date="20260701", close_reason="HARD_STOP",
               entry_at="2026-07-01T09:12:00+09:00",
               exit_at="2026-07-01T09:20:00+09:00"),
        _trade(date="20260702", close_reason="TIMEOUT",
               entry_at="2026-07-02T09:12:00+09:00",
               exit_at="2026-07-02T11:00:00+09:00"),
    ]

    ht = server._improve_from_rows(trades, [], {})["hold_time"]

    assert ht["HARD_STOP"] == {"n": 1, "avg_min": 8.0}
    assert ht["TIMEOUT"] == {"n": 1, "avg_min": 108.0}


def test_improve_slippage_adverse_is_positive_for_both_sides():
    orders = [
        # 매수: 비싸게 체결 = 불리 → +0.3
        _order("FIRST_BUY", 10_000.0, 10_030.0, latency=400),
        # 매도: 싸게 체결 = 불리 → +0.2
        _order("CLOSE_SELL", 10_000.0, 9_980.0, latency=600),
        # 매도: 비싸게 체결 = 유리 → -0.1
        _order("TIMEOUT_SELL", 10_000.0, 10_010.0, latency=800),
    ]

    sl = server._improve_from_rows([], orders, {})["slippage"]

    assert sl["buy"] == {"n": 1, "avg_pp": 0.3, "max_pp": 0.3, "avg_latency_ms": 400}
    assert sl["sell"]["n"] == 2
    assert sl["sell"]["avg_pp"] == 0.05
    assert sl["sell"]["max_pp"] == 0.2
    assert sl["sell"]["avg_latency_ms"] == 700
    assert sl["by_phase"]["FIRST_BUY"]["n"] == 1
    assert sl["by_phase"]["CLOSE_SELL"]["avg_pp"] == 0.2


def test_improve_slippage_skips_rows_without_prices():
    orders = [
        _order("FIRST_BUY", None, 10_030.0),
        _order("FIRST_BUY", 0, 10_030.0),
        _order("FIRST_BUY", 10_000.0, None),
    ]

    sl = server._improve_from_rows([], orders, {})["slippage"]

    assert sl["buy"]["n"] == 0
    assert sl["by_phase"] == {}


def test_improve_slippage_prefers_trigger_price_for_market_order():
    orders = [
        _order("CLOSE_SELL", 0.0, 9_980.0, trigger_price=10_000.0),
    ]

    sl = server._improve_from_rows([], orders, {})["slippage"]

    assert sl["sell"]["n"] == 1
    assert sl["sell"]["avg_pp"] == 0.2


def test_improve_guard_count_and_skips():
    # SLIPPAGE_GUARD는 거래를 열기 전에 daily_skips에 기록되고 반환된다 —
    # 종료 거래(close_reason)에는 존재할 수 없으므로 skips에서 집계해야 한다.
    trades = [_trade(date="20260701", close_reason="TIMEOUT", pnl_pct=-1.2)]
    skips = {"NO_TARGET": 3, "GAP_CHANGED": 1, "SLIPPAGE_GUARD": 1}

    payload = server._improve_from_rows(trades, [], skips)

    assert payload["slippage"]["guard_n"] == 1
    assert payload["candidates"] == {
        "skips": {"NO_TARGET": 3, "GAP_CHANGED": 1, "SLIPPAGE_GUARD": 1},
        "skip_days": 5,
        "trade_days": 1,
    }


def test_improve_echoes_f1_conditional_hard_max():
    payload = server._improve_from_rows([], [], {})

    assert payload["params"]["f1_gap_hard_max_pct"] == 10.0


def test_improve_excludes_manual_trade_from_strategy_diagnostics():
    trades = [
        _trade(date="20260701", pnl_pct=2.0, close_reason="TRAILING", highest_step=0.025),
        _trade(date="20260702", pnl_pct=-5.0, close_reason="MANUAL"),
    ]

    payload = server._improve_from_rows(trades, [], {})

    assert payload["overall"]["total"] == 1
    assert payload["overall"]["expectancy"] == 2.0
    assert payload["data_quality"]["source_closed_n"] == 2
    assert payload["data_quality"]["excluded_manual_n"] == 1


def test_improve_step_rate_excludes_unobservable_legacy_rows():
    trades = [
        _trade(date="20260701", high_price=None, highest_step=0.0),
        _trade(date="20260702", high_price=None, highest_step=0.025),
        _trade(date="20260703", high_price=10_100.0, highest_step=0.0),
    ]

    payload = server._improve_from_rows(trades, [], {})
    step = payload["step"]

    assert step["observed_n"] == 2
    assert step["step1_n"] == 1
    assert step["step1_rate"] == 50.0
    assert step["coverage_pct"] == 66.7
    assert payload["data_quality"]["mfe_observed_n"] == 1


def test_improve_trailing_diagnostic_compares_exit_with_active_step_stop():
    trades = [
        _trade(
            date="20260701", high_price=10_988.0, pnl_pct=5.95,
            close_reason="TRAILING", highest_step=0.075,
        ),
        _trade(
            date="20260702", high_price=11_226.0, pnl_pct=8.58,
            close_reason="TRAILING", highest_step=0.10,
        ),
    ]

    payload = server._improve_from_rows(trades, [], {})
    diag = payload["trailing_diagnostics"]

    assert diag["giveback_n"] == 2
    assert diag["stop_eval_n"] == 2
    assert diag["avg_stop_slip_pp"] == 0.0
    assert diag["max_stop_slip_pp"] == 0.05
    assert diag["structural_giveback_min_pp"] == 1.5
    assert diag["structural_giveback_max_pp"] == 4.0


def test_improve_candidate_supply_uses_no_target_only_and_deduplicates_dates():
    trades = [_trade(date="20260703", close_reason="TIMEOUT", pnl_pct=1.0)]
    skips = {"NO_TARGET": 2, "ENTRY_FAIL": 1, "MARKET_CLOSED": 1}
    skip_rows = [
        {"date": "20260701", "reason": "NO_TARGET"},
        {"date": "20260703", "reason": "NO_TARGET"},
        {"date": "20260704", "reason": "ENTRY_FAIL"},
        {"date": "20260705", "reason": "MARKET_CLOSED"},
    ]

    supply = server._improve_from_rows(
        trades, [], skips, skip_rows=skip_rows,
    )["candidate_supply"]

    assert supply == {
        "trade_days": 1,
        "no_target_days": 1,
        "evaluated_days": 2,
        "operational_skip_n": 1,
        "market_closed_days": 1,
        "overlap_days": 1,
    }


def test_improve_marks_zero_fill_latency_as_unmeasured():
    orders = [_order("FIRST_BUY", 10_000.0, 10_010.0, latency=0)]

    quality = server._improve_from_rows([], orders, {})["data_quality"]

    assert quality["fill_latency_measured_n"] == 0

@pytest.mark.asyncio
async def test_api_improve_empty_db_returns_default_structure(tmp_path):
    await db.init(str(tmp_path / "improve.db"))

    resp = await server.api_improve()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["overall"]["total"] == 0
    assert payload["mfe_rows"] == []
    assert payload["slippage"]["buy"]["n"] == 0
    assert payload["candidates"]["skips"] == {}
    assert payload["params"]["hard_stop_pct"] == 2.0
    await db.close()


@pytest.mark.asyncio
async def test_api_improve_aggregates_seeded_trades_orders_skips(tmp_path):
    await db.init(str(tmp_path / "improve.db"))
    conn = db.get()

    # 거래 1: 손절 (MFE 2.1%, 8분 보유 → 근접 이탈 + 빠른 손절)
    t1 = await db.open_trade("20260701", "005930", 10_000.0, 10, name="삼성전자")
    await db.update_trade_progress(t1, 10_210.0, 0.0)
    await db.close_trade(
        t1, 9_787.0, "HARD_STOP", -2.13, 0.0,
        exit_qty=10, high_price=10_210.0,
    )
    # 거래 2: 트레일링 (스텝1 도달, MFE 4.0%)
    t2 = await db.open_trade("20260702", "000660", 10_000.0, 10, name="SK하이닉스")
    await db.update_trade_progress(t2, 10_400.0, 0.025)
    await db.close_trade(
        t2, 10_100.0, "TRAILING", 1.0, 0.025,
        exit_qty=10, high_price=10_400.0,
    )
    # 진입·청산 시각을 결정적으로 고정 (close_trade는 now를 기록)
    await conn.execute(
        "UPDATE trades SET entry_at=?, exit_at=? WHERE id=?",
        ("2026-07-01T09:12:00+09:00", "2026-07-01T09:20:00+09:00", t1))
    await conn.execute(
        "UPDATE trades SET entry_at=?, exit_at=? WHERE id=?",
        ("2026-07-02T09:12:00+09:00", "2026-07-02T10:30:00+09:00", t2))
    await conn.commit()

    # 주문: 매수 불리 +0.3%p 체결
    o1 = await db.record_order(t1, "KIS001", "BUY", 10, 10_000.0,
                               "FIRST_BUY", "005930", name="삼성전자")
    await db.update_order_fill(o1, 10_030.0, 10, 400)
    # 미체결(PENDING) 주문은 집계에서 제외되어야 함
    await db.record_order(t1, "KIS002", "SELL", 10, 10_000.0,
                          "CLOSE_SELL", "005930")

    await db.record_skip("20260703", "NO_TARGET", "후보 없음")
    await db.record_skip("20260704", "SLIPPAGE_GUARD", "expected=10000,fill=10800")

    resp = await server.api_improve()
    payload = json.loads(resp.body.decode("utf-8"))

    assert payload["overall"]["total"] == 2
    assert payload["step"]["step1_n"] == 1
    assert payload["step"]["near_miss_n"] == 1
    assert payload["hard_stop"]["n"] == 1
    assert payload["hard_stop"]["fast_stop_n"] == 1
    assert payload["mfe_rows"][0]["date"] == "20260702"  # 최신 먼저
    assert payload["slippage"]["buy"]["n"] == 1
    assert payload["slippage"]["buy"]["avg_pp"] == 0.3
    assert payload["slippage"]["guard_n"] == 1
    assert payload["candidates"]["skips"] == {"NO_TARGET": 1, "SLIPPAGE_GUARD": 1}
    assert payload["candidates"]["trade_days"] == 2
    await db.close()
