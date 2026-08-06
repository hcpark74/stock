"""Read-only safety check used by the Windows main-process restart script."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

KST = ZoneInfo("Asia/Seoul")
SAFE_STATES = frozenset({"IDLE", "CLOSED"})


def _runtime_paths(
    root: Path,
    environ: Mapping[str, str] | None = None,
) -> tuple[Path, Path]:
    """Resolve paths with the same .env and DRY_RUN rules as main.py."""
    file_values = {
        key: value
        for key, value in dotenv_values(root / ".env").items()
        if value is not None
    }
    effective_env = {**file_values, **(os.environ if environ is None else environ)}
    if effective_env.get("DRY_RUN", "0") == "1":
        state_dir = effective_env.get("DRY_RUN_STATE_DIR", "data/dry_run/state")
        db_dir = effective_env.get("DRY_RUN_DB_DIR", "data/dry_run/db")
    else:
        state_dir = effective_env.get("STATE_DIR", "data/state")
        db_dir = effective_env.get("DB_DIR", "data/db")

    def rooted(path_text: str) -> Path:
        path = Path(path_text)
        return path if path.is_absolute() else root / path

    return rooted(state_dir), rooted(db_dir) / "trading.db"


def inspect_restart_safety(
    root: Path,
    *,
    environ: Mapping[str, str] | None = None,
    today: str | None = None,
) -> dict:
    state_dir, db_path = _runtime_paths(root, environ)
    state_path = state_dir / "today_state.json"
    today = today or datetime.now(KST).strftime("%Y%m%d")
    reasons: list[str] = []
    state_status: str | None = None
    pending_entry = False
    open_trades: list[dict] = []

    if state_path.exists():
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(state_data, dict):
                raise TypeError("state root must be an object")
            state_status = str(state_data.get("position_status") or "UNKNOWN").upper()
            pending_entry = bool(state_data.get("pending_entry"))
            if state_status not in SAFE_STATES:
                reasons.append(f"UNSAFE_STATE:{state_status}")
            if pending_entry:
                reasons.append("PENDING_ENTRY")
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            reasons.append(f"STATE_UNREADABLE:{exc.__class__.__name__}")

    if db_path.exists():
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
            connection.row_factory = sqlite3.Row
            try:
                rows = connection.execute(
                    """
                    SELECT id, date, ticker, name, entry_qty, status
                    FROM trades
                    WHERE date = ? AND status = 'OPEN'
                    ORDER BY id DESC
                    LIMIT 10
                    """,
                    (today,),
                ).fetchall()
                open_trades = [dict(row) for row in rows]
            finally:
                connection.close()
            if open_trades:
                reasons.append(f"OPEN_TRADES:{len(open_trades)}")
        except (sqlite3.Error, OSError) as exc:
            reasons.append(f"DB_UNREADABLE:{exc.__class__.__name__}")

    return {
        "safe": not reasons,
        "reasons": reasons,
        "state_path": str(state_path),
        "state_status": state_status,
        "pending_entry": pending_entry,
        "open_trades": open_trades,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    result = inspect_restart_safety(args.root.resolve())
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["safe"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
