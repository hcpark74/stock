"""전략 코드 지문 생성과 프로세스 수명 캐시."""

import pytest

from src import release

_REQUIRED_ORDER_DECISION_FILES = {
    "main.py",
    "src/live.py",
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

# 관측 전용 모듈. 지문에 넣으면 관측 코드 수정마다 새 기준선이 열려 40거래일
# paired 수집이 초기화되므로 반드시 제외한다.
_OBSERVATION_ONLY_FILES = {
    "src/modules/tick_capture.py",
    "src/modules/f1_snapshot_selector.py",
}


def test_strategy_fingerprint_file_list_covers_order_decision_dependencies():
    assert _REQUIRED_ORDER_DECISION_FILES <= set(release._STRATEGY_FILES)


def test_strategy_fingerprint_excludes_observation_only_modules():
    assert not (_OBSERVATION_ONLY_FILES & set(release._STRATEGY_FILES))


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


def test_strategy_fingerprint_changes_with_loaded_strategy_environment(monkeypatch):
    monkeypatch.setenv("F1_GAP_MIN", "0.025")
    release.strategy_fingerprint.cache_clear()
    try:
        baseline = release.strategy_fingerprint()
        monkeypatch.setenv("F1_GAP_MIN", "0.030")
        # 프로세스 수명 중에는 시작 지문을 고정한다.
        assert release.strategy_fingerprint() == baseline
        release.strategy_fingerprint.cache_clear()
        assert release.strategy_fingerprint() != baseline
    finally:
        release.strategy_fingerprint.cache_clear()


def test_strategy_fingerprint_covers_tick_capture_env(monkeypatch):
    monkeypatch.setenv("STRATEGY_TICK_CAPTURE_ENABLED", "1")
    release.strategy_fingerprint.cache_clear()
    try:
        baseline = release.strategy_fingerprint()
        monkeypatch.setenv("STRATEGY_TICK_CAPTURE_ENABLED", "0")
        release.strategy_fingerprint.cache_clear()
        assert release.strategy_fingerprint() != baseline
    finally:
        release.strategy_fingerprint.cache_clear()


def test_strategy_fingerprint_excludes_secrets(monkeypatch):
    monkeypatch.setenv("KIS_APP_SECRET", "secret-a")
    release.strategy_fingerprint.cache_clear()
    try:
        baseline = release.strategy_fingerprint()
        monkeypatch.setenv("KIS_APP_SECRET", "secret-b")
        release.strategy_fingerprint.cache_clear()
        assert release.strategy_fingerprint() == baseline
    finally:
        release.strategy_fingerprint.cache_clear()


def test_strategy_fingerprint_ignores_launcher_env(monkeypatch):
    """stock.bat 런처가 설정하는 환경변수는 지문을 바꾸지 않아야 한다.

    런처(scripts/start_main.ps1)는 PYTHONUTF8/PYTHONUNBUFFERED만 설정한다.
    여기에 전략 환경변수를 추가하면 지문이 바뀌어 PAPER 실적이 리셋된다.
    """
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONUNBUFFERED", raising=False)
    release.strategy_fingerprint.cache_clear()
    try:
        baseline = release.strategy_fingerprint()
        monkeypatch.setenv("PYTHONUTF8", "1")
        monkeypatch.setenv("PYTHONUNBUFFERED", "1")
        release.strategy_fingerprint.cache_clear()
        assert release.strategy_fingerprint() == baseline
    finally:
        release.strategy_fingerprint.cache_clear()
