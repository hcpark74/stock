"""현재 자동매매 로직 버전의 재현 가능한 지문."""

from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_STRATEGY_FILES = (
    "main.py",
    "src/state.py",
    "src/live.py",
    "src/db.py",
    "src/api/kis_rest.py",
    "src/api/kis_ws.py",
    "src/modules/f1_filter.py",
    "src/modules/f1_selector.py",
    "src/modules/f2_lockup.py",
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
)

# 비밀키·계좌·파일 경로는 제외하고, 선택·진입·청산 및 그 실행 결과에 영향을
# 주는 환경설정만 지문에 포함한다. 코드 기본값 변경은 _STRATEGY_FILES 해시가,
# 배포별 override 변경은 이 환경 스냅샷이 포착한다.
_STRATEGY_ENV_PREFIXES = (
    "F1_",
    "F2_",
    "F3_",
    "F4_",
    "F5_",
    "PAPER_FAST_",
    "TRAILING_SHADOW_",
    "VI_",
    "BALANCE_SNAPSHOT_",
    "EXIT_RECONCILE_",
    "KIS_RATE_",
    "KIS_MAX_TRANSIENT_",
    "KIS_TRANSIENT_",
    "KIS_LOW_PRIORITY_",
)
_STRATEGY_ENV_NAMES = {
    "FORCE_CATCHUP",
}


def _strategy_environment() -> dict[str, str]:
    """현재 프로세스에 실제 로드된 비밀값 제외 전략 override를 반환한다."""
    return {
        name: value.strip()
        for name, value in sorted(os.environ.items())
        if name in _STRATEGY_ENV_NAMES
        or any(name.startswith(prefix) for prefix in _STRATEGY_ENV_PREFIXES)
    }


@lru_cache(maxsize=1)
def strategy_fingerprint() -> str:
    """주문 판단·상태 복구 코드나 유효 환경설정이 바뀌는 12자리 SHA-256."""
    digest = hashlib.sha256()
    for relative in _STRATEGY_FILES:
        path = _ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        # 목록에 선언된 전략 파일을 읽지 못한 상태에서 별도의 유효 지문을
        # 만들면 준비도 실적이 잘못 이어질 수 있으므로 기동을 fail-closed한다.
        digest.update(path.read_bytes())
        digest.update(b"\0")
    digest.update(b"strategy-environment\0")
    digest.update(
        json.dumps(
            _strategy_environment(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )
    digest.update(b"\0")
    return digest.hexdigest()[:12]
