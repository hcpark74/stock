import pytest

pytest.importorskip("fastapi")

import src.api.server as server  # noqa: E402 — fastapi 미설치 시 모듈 스킵 이후 임포트


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
