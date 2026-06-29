"""최초 1회 실행 — 데이터 디렉토리 및 초기 파일 생성."""

import json
from pathlib import Path

DIRS = [
    "data/logs",
    "data/state",
    "data/params",
    "data/auth",
    "tests/fixtures",
]


def main() -> None:
    for d in DIRS:
        Path(d).mkdir(parents=True, exist_ok=True)
        print(f"[OK] {d}/")

    params_file = Path("data/params/history.json")
    if not params_file.exists():
        params_file.write_text(json.dumps([], ensure_ascii=False), encoding="utf-8")
        print("[OK] data/params/history.json (초기화)")
    else:
        print("[SKIP] data/params/history.json (이미 존재)")

    print("\n디렉토리 초기화 완료.")


if __name__ == "__main__":
    main()
