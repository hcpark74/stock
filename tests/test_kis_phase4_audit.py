import json
import sqlite3
from datetime import datetime

import pytest

from scripts import kis_phase4_readonly_audit as audit


def test_safe_window_rejects_entry_window_and_before_close():
    with pytest.raises(audit.AuditStop, match="FORBIDDEN_0900_0911"):
        audit._assert_safe_window(datetime(2026, 7, 21, 9, 5, tzinfo=audit.KST))

    with pytest.raises(audit.AuditStop, match="AFTER_CLOSE_ONLY"):
        audit._assert_safe_window(datetime(2026, 7, 21, 15, 39, tzinfo=audit.KST))

    audit._assert_safe_window(datetime(2026, 7, 21, 15, 40, tzinfo=audit.KST))


@pytest.mark.parametrize("msg_cd", ["EGW00201", "HTTP_429"])
def test_assert_success_stops_on_rate_limit(msg_cd):
    with pytest.raises(audit.AuditStop) as exc_info:
        audit._assert_success({"rt_cd": "1", "msg_cd": msg_cd}, "BALANCE")

    assert exc_info.value.reason == "RATE_LIMIT"
    assert exc_info.value.msg_cd == msg_cd


def test_read_db_and_log_order_ids_without_exposing_payload(tmp_path):
    db_path = tmp_path / "trading.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE orders (kis_order_id TEXT, status TEXT, ordered_at TEXT)"
        )
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?)",
            [
                ("1001", "FILLED", "2026-07-21T09:10:00+09:00"),
                ("1002", "PENDING", "2026-07-21T09:10:01+09:00"),
                ("9999", "PENDING", "2026-07-20T09:10:01+09:00"),
            ],
        )

    all_ids, pending_ids, row_count = audit._read_db_orders(db_path, "2026-07-21")

    assert all_ids == {"1001", "1002"}
    assert pending_ids == {"1002"}
    assert row_count == 2

    log_path = tmp_path / "20260721.jsonl"
    log_path.write_text(
        "\n".join(
            [
                json.dumps({"event": "ENTRY_ORDER_SENT", "order_id": "1001"}),
                "not-json",
                json.dumps({"event": "ENTRY_CANCEL_SENT", "kis_order_id": "1002"}),
            ]
        ),
        encoding="utf-8",
    )

    log_ids, event_count = audit._read_log_order_ids(log_path)

    assert log_ids == {"1001", "1002"}
    assert event_count == 2
