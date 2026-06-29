"""공통 설정 — 각 테스트 파일에서 가장 먼저 import."""
import os
import sys
from pathlib import Path

# Windows 터미널 인코딩을 UTF-8로 강제
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# 어디서 실행해도 프로젝트 루트를 sys.path에 추가
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")


def mode() -> str:
    return os.getenv("KIS_MODE", "PAPER")

def acct_no() -> str:
    return os.getenv("KIS_ACCT_NO", "")

def acct_cd() -> str:
    return os.getenv("KIS_ACCT_CD", "01")

def header(title: str) -> None:
    bar = "-" * 54
    print(f"\n{bar}")
    print(f"  {title}")
    print(bar)

def ok(label: str, detail: str = "") -> None:
    suffix = f"  <- {detail}" if detail else ""
    print(f"  [PASS] {label}{suffix}")

def fail(label: str, detail: str = "") -> None:
    suffix = f"  <- {detail}" if detail else ""
    print(f"  [FAIL] {label}{suffix}")
