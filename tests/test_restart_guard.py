import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from scripts.restart_guard import inspect_restart_safety

KST = ZoneInfo("Asia/Seoul")
TODAY = datetime.now(KST).strftime("%Y%m%d")


def _make_db(path, *, status: str | None = None, date: str = TODAY) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY,
            date TEXT,
            ticker TEXT,
            name TEXT,
            entry_qty INTEGER,
            status TEXT
        )
        """
    )
    if status is not None:
        connection.execute(
            "INSERT INTO trades VALUES (1, ?, '005930', '삼성전자', 10, ?)",
            (date, status),
        )
    connection.commit()
    connection.close()


def test_restart_guard_allows_idle_or_missing_state_without_open_trade(tmp_path):
    _make_db(tmp_path / "data" / "db" / "trading.db", status="CLOSED")

    missing = inspect_restart_safety(tmp_path, environ={})
    assert missing["safe"] is True

    state_path = tmp_path / "data" / "state" / "today_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"position_status": "IDLE"}), encoding="utf-8")

    idle = inspect_restart_safety(tmp_path, environ={})
    assert idle["safe"] is True
    assert idle["state_status"] == "IDLE"


def test_restart_guard_blocks_active_state_pending_entry_and_open_trade(tmp_path):
    _make_db(tmp_path / "data" / "db" / "trading.db", status="OPEN")
    state_path = tmp_path / "data" / "state" / "today_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps({
            "position_status": "ENTERING",
            "pending_entry": {"order_id": "O1"},
        }),
        encoding="utf-8",
    )

    result = inspect_restart_safety(tmp_path, environ={})

    assert result["safe"] is False
    assert result["reasons"] == ["UNSAFE_STATE:ENTERING", "PENDING_ENTRY", "OPEN_TRADES:1"]
    assert result["open_trades"][0]["ticker"] == "005930"


def test_restart_guard_fails_closed_on_invalid_state_or_database(tmp_path):
    state_path = tmp_path / "data" / "state" / "today_state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{broken", encoding="utf-8")
    db_path = tmp_path / "data" / "db" / "trading.db"
    db_path.parent.mkdir(parents=True)
    db_path.write_text("not sqlite", encoding="utf-8")

    result = inspect_restart_safety(tmp_path, environ={})

    assert result["safe"] is False
    assert result["reasons"] == [
        "STATE_UNREADABLE:JSONDecodeError",
        "DB_UNREADABLE:DatabaseError",
    ]


def test_restart_guard_uses_runtime_env_paths_and_dry_run_overrides(tmp_path):
    real_state = tmp_path / "custom" / "state"
    real_state.mkdir(parents=True)
    (real_state / "today_state.json").write_text(
        json.dumps({"position_status": "HOLDING"}), encoding="utf-8"
    )
    _make_db(tmp_path / "custom" / "db" / "trading.db", status="OPEN")

    result = inspect_restart_safety(
        tmp_path,
        environ={"STATE_DIR": "custom/state", "DB_DIR": "custom/db"},
    )

    assert result["safe"] is False
    assert result["reasons"] == ["UNSAFE_STATE:HOLDING", "OPEN_TRADES:1"]
    assert result["state_path"] == str(real_state / "today_state.json")

    dry_state = tmp_path / "sandbox" / "state"
    dry_state.mkdir(parents=True)
    (dry_state / "today_state.json").write_text(
        json.dumps({"position_status": "CLOSED"}), encoding="utf-8"
    )
    _make_db(tmp_path / "sandbox" / "db" / "trading.db", status="CLOSED")

    dry_result = inspect_restart_safety(
        tmp_path,
        environ={
            "DRY_RUN": "1",
            "STATE_DIR": "wrong/state",
            "DB_DIR": "wrong/db",
            "DRY_RUN_STATE_DIR": "sandbox/state",
            "DRY_RUN_DB_DIR": "sandbox/db",
        },
    )

    assert dry_result["safe"] is True
    assert dry_result["state_path"] == str(dry_state / "today_state.json")


def test_restart_guard_rejects_unknown_state_but_allows_only_idle_closed(tmp_path):
    _make_db(tmp_path / "data" / "db" / "trading.db", status="CLOSED")
    state_path = tmp_path / "data" / "state" / "today_state.json"
    state_path.parent.mkdir(parents=True)

    state_path.write_text(json.dumps({"position_status": "PAUSED"}), encoding="utf-8")
    unknown = inspect_restart_safety(tmp_path, environ={})
    assert unknown["safe"] is False
    assert unknown["reasons"] == ["UNSAFE_STATE:PAUSED"]

    state_path.write_text(json.dumps({"position_status": "CLOSED"}), encoding="utf-8")
    closed = inspect_restart_safety(tmp_path, environ={})
    assert closed["safe"] is True


def test_restart_guard_only_blocks_todays_open_trade(tmp_path):
    yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")
    _make_db(
        tmp_path / "data" / "db" / "trading.db",
        status="OPEN",
        date=yesterday,
    )

    old_open = inspect_restart_safety(tmp_path, environ={}, today=TODAY)
    assert old_open["safe"] is True

    db_path = tmp_path / "data" / "db" / "trading.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "INSERT INTO trades VALUES (2, ?, '000660', 'SK하이닉스', 5, 'SKIPPED')",
        (TODAY,),
    )
    connection.commit()
    connection.close()

    skipped = inspect_restart_safety(tmp_path, environ={}, today=TODAY)
    assert skipped["safe"] is True
