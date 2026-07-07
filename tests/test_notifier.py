import asyncio

import pytest

from src import notifier


def test_alert_text_is_simple_three_line_format():
    text = notifier._format_alert_text(
        "ENTRY_EXECUTED",
        level="INFO",
        message="진입: 005930 10주 @ 75,000원",
        ticker="005930",
        name="삼성전자",
    )

    assert text == "\n".join([
        "[알림] 매수 체결",
        "종목 : 005930 삼성전자",
        "내용 : 진입: 005930 10주 @ 75,000원",
    ])


def test_unknown_alert_uses_event_title_and_empty_stock():
    text = notifier._format_alert_text("SOME_NEW_EVENT", level="WARN")

    assert text == "\n".join([
        "[확인] Some New Event",
        "종목 : -",
        "내용 : Some New Event",
    ])


def test_format_stock_accepts_code_only_and_name_only():
    code_only = notifier._format_alert_text(
        "TRAILING_STOP",
        level="INFO",
        message="TRAILING 청산",
        ticker="005930",
    )
    name_only = notifier._format_alert_text(
        "TRAILING_STOP",
        level="INFO",
        message="TRAILING 청산",
        name="삼성전자",
    )

    assert "종목 : 005930" in code_only
    assert "종목 : 삼성전자" in name_only


@pytest.mark.asyncio
async def test_send_filters_non_actionable_alerts():
    notifier._queue = asyncio.Queue()

    await notifier.send("TARGET_LOCKED", level="INFO", message="target locked")
    await notifier.send("F2_FAIL_F1_RETRY", level="WARN", message="retrying")

    assert notifier._queue.empty()


@pytest.mark.asyncio
async def test_send_allows_trade_no_trade_and_failure_alerts():
    notifier._queue = asyncio.Queue()

    await notifier.send("ENTRY_EXECUTED", level="INFO", message="buy filled", ticker="005930")
    await notifier.send("TRAILING_STOP", level="INFO", message="sell filled", ticker="005930")
    await notifier.send("ENTRY_FAIL", level="WARN", message="entry failed", ticker="005930")
    await notifier.send("NO_TARGET", level="INFO", message="no target")
    await notifier.send("VI_FILTER_ALL_EXCLUDED", level="WARN", message="vi excluded")
    await notifier.send("GAP_CHANGED", level="WARN", message="gap changed", ticker="005930")
    await notifier.send("F2_RETRY_EXHAUSTED", level="WARN", message="no final target")
    await notifier.send("TIMEOUT_ORDER_FAILED", level="CRIT", message="manual close required")

    assert notifier._queue.qsize() == 8


@pytest.mark.asyncio
async def test_send_allows_error_level_alerts():
    notifier._queue = asyncio.Queue()

    await notifier.send("SOME_ERROR", level="ERROR", message="boom")

    assert notifier._queue.qsize() == 1


@pytest.mark.asyncio
async def test_process_restart_alert_requires_critical_level():
    notifier._queue = asyncio.Queue()

    await notifier.send("PROCESS_RESTART_DETECTED", level="WARN", message="restart")
    assert notifier._queue.empty()

    await notifier.send("PROCESS_RESTART_DETECTED", level="CRIT", message="restart")
    assert notifier._queue.qsize() == 1


def test_notifier_accepts_legacy_and_standard_error_levels():
    assert notifier._should_send_alert("SOME_ERROR", level="CRIT") is True
    assert notifier._should_send_alert("SOME_ERROR", level="error") is True
    assert notifier._should_send_alert("SOME_WARNING", level="WARN") is False
