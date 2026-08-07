"""현재 자동매매 로직 버전의 재현 가능한 지문."""

from __future__ import annotations

import hashlib
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


@lru_cache(maxsize=1)
def strategy_fingerprint() -> str:
    """주문 판단·상태 복구 코드가 바뀌면 달라지는 12자리 SHA-256."""
    digest = hashlib.sha256()
    for relative in _STRATEGY_FILES:
        path = _ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        # 목록에 선언된 전략 파일을 읽지 못한 상태에서 별도의 유효 지문을
        # 만들면 준비도 실적이 잘못 이어질 수 있으므로 기동을 fail-closed한다.
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()[:12]
