import logging

from src.utils.logger import log, normalize_level


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