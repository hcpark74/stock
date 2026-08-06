"""현재 실전투자 준비도를 콘솔에서 확인한다."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import db  # noqa: E402
from src.readiness import calculate  # noqa: E402


async def _main() -> None:
    load_dotenv()
    db_path = os.path.join(os.getenv("DB_DIR", "data/db"), "trading.db")
    await db.init(db_path)
    try:
        print(json.dumps(await calculate(), ensure_ascii=False, indent=2))
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(_main())
