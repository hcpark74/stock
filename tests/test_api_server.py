from pathlib import Path

import pytest

pytest.importorskip("fastapi")

import src.api.server as server
import src.modules.f1_filter as f1_filter


def test_server_uses_f1_snapshot_dir_constant():
    assert server._F1_SNAPSHOT_DIR == Path(f1_filter.F1_SNAPSHOT_DIR)


def test_f1_verdict_has_high_gap_vi_unknown_label():
    assert server._f1_verdict({"gap_reason": "HIGH_GAP_VI_UNKNOWN"}) == "VI미확인"


def test_f1_snapshot_saved_is_only_weak_done_signal():
    logs = [
        {"event": "F1_SNAPSHOT_SAVED"},
        {"event": "F1_RETRY_WAIT"},
    ]

    status, last_event = server._f1_status_from_logs(logs)

    assert status == "RETRYING"
    assert last_event == logs[-1]


def test_f1_summary_uses_same_liquidity_selection_as_f1_filter():
    rows = [
        {
            "ticker": f"TICK{i:02d}",
            "gap_allowed": True,
            "gap_pct": 0.05,
            "avg_amount_5d": float(i) * 1e9,
        }
        for i in range(1, 21)
    ]

    summary = server._f1_summary_from_rows(rows)

    assert summary["liquidity_pass"] == 2
    assert summary["selected"]["ticker"] == "TICK20"


def test_f1_candidates_display_pass_candidates_before_ranking_order():
    rows = [
        {
            "ticker": f"DROP{i:02d}",
            "gap_allowed": False,
            "gap_pct": 0.0,
            "expected_amount": float(100 - i) * 1e8,
        }
        for i in range(20)
    ]
    rows.append(
        {
            "ticker": "PASS01",
            "gap_allowed": True,
            "gap_pct": 0.05,
            "ranking_gap_pct": 0.0,
            "expected_api_gap_pct": 0.05,
            "expected_api_price": 10500,
            "expected_api_qty": 10,
            "expected_amount": 1e8,
            "avg_amount_5d": 1e8,
        }
    )

    summary = server._f1_summary_from_rows(rows)

    assert summary["gap_pass"] == 1
    assert summary["candidates"][0]["ticker"] == "PASS01"


def test_pipeline_uses_today_logs_after_entry_fail_returns_to_idle():
    logs = [
        {"event": "F1_DONE"},
        {"event": "TARGET_LOCKED"},
        {"event": "ENTRY_ORDER_SENT"},
        {"event": "ENTRY_FAIL"},
    ]

    pipeline = server._pipeline_from_logs(logs, "IDLE")

    assert pipeline == {"pipeline_stage": 2, "pipeline_failed": True}


def test_pipeline_live_position_status_takes_precedence():
    logs = [
        {"event": "F1_DONE"},
        {"event": "TARGET_LOCKED"},
        {"event": "ENTRY_FAIL"},
    ]

    pipeline = server._pipeline_from_logs(logs, "HOLDING")

    assert pipeline == {"pipeline_stage": 3, "pipeline_failed": False}
