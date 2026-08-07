import logging

from src.utils.logger import event_label, log, normalize_level


def test_normalize_level_maps_legacy_names_to_standard_levels():
    assert normalize_level("INFO") == "info"
    assert normalize_level("WARN") == "warn"
    assert normalize_level("WARNING") == "warn"
    assert normalize_level("ERROR") == "error"
    assert normalize_level("CRIT") == "error"
    assert normalize_level("CRITICAL") == "error"
    assert normalize_level("DEBUG") == "info"
    assert normalize_level(None) == "info"


def test_log_records_normalized_level_and_python_severity():
    records = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("stock")
    handler = _CaptureHandler()
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        log("TOKEN_REFRESH_FAIL", level="CRIT")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    record = records[-1]
    assert record.levelno == logging.ERROR
    assert record._extra["level"] == "error"


def test_log_infers_current_target_name():
    from src import state

    state.get().target_ticker = "005930"
    state.get().target_name = "삼성전자"
    records = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger("stock")
    handler = _CaptureHandler()
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    try:
        log("ENTRY_EXECUTED", ticker="005930")
        log("VI_DETECTED", ticker="000660")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        state.get().target_ticker = None
        state.get().target_name = None

    assert records[-2]._extra["name"] == "삼성전자"
    assert records[-1]._extra["name"] is None


def test_f3_operational_events_have_human_readable_labels():
    assert event_label("F3_FINAL_PICK") != "F3_FINAL_PICK(F3_FINAL_PICK)"
    assert event_label("F3_RECHECK_BATCH_TIMING") != (
        "F3_RECHECK_BATCH_TIMING(F3_RECHECK_BATCH_TIMING)"
    )
    assert event_label("F4_ENTRY_AT_INVALID") != (
        "F4_ENTRY_AT_INVALID(F4_ENTRY_AT_INVALID)"
    )
    for event in (
        "VI_ENTRY_WAIT_STARTED",
        "VI_ENTRY_RELEASED",
        "VI_ENTRY_WAIT_TIMEOUT",
    ):
        assert event_label(event) != f"{event}({event})"


def test_startup_and_real_gate_events_have_human_readable_labels():
    for event in (
        "STARTUP_RECOVERY_FAILED",
        "STARTUP_RECOVERY_FALLBACK_FAILED",
        "STARTUP_RECOVERY_ALERT_FAILED",
        "STRATEGY_FINGERPRINT_LOCKED",
        "REAL_SMOKE_BUY_AUTHORIZED",
    ):
        assert event_label(event) != f"{event}({event})"


def test_terminal_persist_and_candidate_events_have_distinct_labels():
    # 후보 소진은 마감초과(ENTRY_CANDIDATE_RETRY_SKIPPED)와 별개 이벤트/라벨이다
    exhausted = event_label("ENTRY_CANDIDATE_EXHAUSTED")
    assert exhausted != "ENTRY_CANDIDATE_EXHAUSTED(ENTRY_CANDIDATE_EXHAUSTED)"
    assert "마감" not in exhausted
    assert event_label("ENTRY_CANDIDATE_RETRY_SKIPPED") != exhausted
    # 종료 상태 영속화 실패는 전용 라벨을 가진다
    terminal = event_label("ENTRY_TERMINAL_PERSIST_ERROR")
    assert terminal != "ENTRY_TERMINAL_PERSIST_ERROR(ENTRY_TERMINAL_PERSIST_ERROR)"
