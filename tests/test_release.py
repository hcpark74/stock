"""전략 코드 지문 생성과 프로세스 수명 캐시."""

import pytest

from src import release

_REQUIRED_ORDER_DECISION_FILES = {
    "main.py",
    "src/api/kis_rest.py",
    "src/api/kis_ws.py",
    "src/modules/f3_entry.py",
    "src/modules/f4_tracking.py",
    "src/modules/f5_timeout.py",
    "src/modules/exit_recovery.py",
    "src/modules/paper_fast_probe.py",
    "src/modules/vi_watch.py",
    "src/schedule_times.py",
    "src/scheduler.py",
    "src/utils/number.py",
    "src/utils/spike_filter.py",
}


def test_strategy_fingerprint_file_list_covers_order_decision_dependencies():
    assert _REQUIRED_ORDER_DECISION_FILES <= set(release._STRATEGY_FILES)


def test_strategy_fingerprint_is_frozen_until_process_cache_is_cleared(
    tmp_path,
    monkeypatch,
):
    strategy = tmp_path / "strategy.py"
    strategy.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setattr(release, "_ROOT", tmp_path)
    monkeypatch.setattr(release, "_STRATEGY_FILES", ("strategy.py",))
    release.strategy_fingerprint.cache_clear()
    try:
        started_with = release.strategy_fingerprint()
        strategy.write_text("version = 2\n", encoding="utf-8")

        assert release.strategy_fingerprint() == started_with

        release.strategy_fingerprint.cache_clear()
        assert release.strategy_fingerprint() != started_with
    finally:
        release.strategy_fingerprint.cache_clear()


def test_strategy_fingerprint_fails_closed_when_listed_file_is_missing(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(release, "_ROOT", tmp_path)
    monkeypatch.setattr(release, "_STRATEGY_FILES", ("missing.py",))
    release.strategy_fingerprint.cache_clear()
    try:
        with pytest.raises(FileNotFoundError):
            release.strategy_fingerprint()
    finally:
        release.strategy_fingerprint.cache_clear()
