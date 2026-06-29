"""
프로세스 워치독 — Windows Task Scheduler에서 1분 간격으로 호출 (PRD §6-7).
main.py 프로세스 사망 감지 시 자동 재시작.
10:01 이후에는 재시작하지 않음.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).parent.parent
PID_FILE = ROOT / "main.pid"
MAIN_SCRIPT = ROOT / "main.py"
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
CUTOFF = (10, 1)  # 10시 01분 이후 재시작 안 함


def _is_running(pid: int) -> bool:
    """Windows tasklist로 PID 생존 확인 (psutil 불필요)."""
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True,
        )
        return str(pid) in result.stdout
    except Exception:
        return False


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:
        return None


def main() -> None:
    now = datetime.now(KST)
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")

    if (now.hour, now.minute) >= CUTOFF:
        print(f"[{stamp}] 장 종료 후 — 재시작 안 함")
        return

    pid = _read_pid()
    if pid and _is_running(pid):
        print(f"[{stamp}] 프로세스 정상 실행 중 (PID={pid})")
        return

    print(f"[{stamp}] 프로세스 사망 감지 — 재시작")
    python_exe = str(PYTHON) if PYTHON.exists() else sys.executable
    proc = subprocess.Popen(
        [python_exe, str(MAIN_SCRIPT)],
        cwd=str(ROOT),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    PID_FILE.write_text(str(proc.pid))
    print(f"[{stamp}] 재시작 완료 (PID={proc.pid})")


if __name__ == "__main__":
    main()
