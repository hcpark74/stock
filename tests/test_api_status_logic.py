import os

import src.api.status_logic as status_logic


def test_f1_verdict_has_high_gap_vi_unknown_label():
    assert status_logic.f1_verdict({"gap_reason": "HIGH_GAP_VI_UNKNOWN"}) == "VI미확인"


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

    summary = status_logic.f1_summary_from_rows(rows)

    assert summary["liquidity_pass"] == 10
    assert summary["selected"]["ticker"] == "TICK20"


def test_latest_today_snapshot_ignores_previous_day_files(tmp_path):
    old = tmp_path / "20260701_160253.jsonl"
    old.write_text("{}", encoding="utf-8")

    assert status_logic.latest_today_snapshot_path(tmp_path, "20260702") is None


def test_latest_today_snapshot_selects_newest_today_file(tmp_path):
    first = tmp_path / "20260702_084000.jsonl"
    second = tmp_path / "20260702_085000.jsonl"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    assert status_logic.latest_today_snapshot_path(tmp_path, "20260702") == second


def test_latest_today_snapshot_breaks_mtime_ties_by_filename(tmp_path):
    """Filenames sort chronologically; mtime alone can tie when writes land in the same tick."""
    first = tmp_path / "20260702_084000.jsonl"
    second = tmp_path / "20260702_085000.jsonl"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    tied_time = first.stat().st_mtime
    os.utime(second, (tied_time, tied_time))

    assert status_logic.latest_today_snapshot_path(tmp_path, "20260702") == second


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

    summary = status_logic.f1_summary_from_rows(rows)

    assert summary["gap_pass"] == 1
    assert summary["candidates"][0]["ticker"] == "PASS01"


def test_pipeline_uses_today_logs_after_entry_fail_returns_to_idle():
    logs = [
        {"event": "F1_DONE"},
        {"event": "TARGET_LOCKED"},
        {"event": "ENTRY_ORDER_SENT"},
        {"event": "ENTRY_FAIL"},
    ]

    pipeline = status_logic.pipeline_from_logs(logs, "IDLE")

    assert pipeline == {"pipeline_stage": 2, "pipeline_failed": True}


def test_pipeline_live_position_status_takes_precedence():
    logs = [
        {"event": "F1_DONE"},
        {"event": "TARGET_LOCKED"},
        {"event": "ENTRY_FAIL"},
    ]

    pipeline = status_logic.pipeline_from_logs(logs, "HOLDING")

    assert pipeline == {"pipeline_stage": 3, "pipeline_failed": False}


def test_f3_detail_from_event_labels_internal_reasons():
    assert status_logic.f3_detail_from_event({"event": "GAP_CHANGED", "reason": "BELOW_MIN"}) == "갭 하한 미달"
    assert status_logic.f3_detail_from_event({"event": "GAP_CHANGED", "reason": "ABOVE_MAX"}) == "갭 상한 초과"
    assert status_logic.f3_detail_from_event({"event": "F3_ENTRY_BLOCKED", "reason": "PRICE_UNAVAILABLE"}) == "예상가 조회 실패"


def test_f3_detail_from_event_summarizes_final_pick():
    assert (
        status_logic.f3_detail_from_event({
            "event": "F3_FINAL_PICK",
            "valid_count": 2,
            "checked_count": 3,
        })
        == "2 / 3 재검증"
    )


def test_parse_asset_snapshot_response_parses_kis_balance():
    result = status_logic.parse_asset_snapshot_response({
        "rt_cd": "0",
        "output1": [
            {"pdno": "005930", "hldg_qty": "2"},
            {"pdno": "000660", "hldg_qty": "0"},
        ],
        "output2": [{
            "dnca_tot_amt": "1,000,000",
            "ord_psbl_cash": "800000",
            "scts_evlu_amt": "500000",
            "tot_evlu_amt": "1500000",
            "evlu_pfls_smtl_amt": "12000",
        }],
    })

    assert result == {
        "cash": 1_000_000.0,
        "buyable_cash": 800_000.0,
        "buyable_cash_source": "ord_psbl_cash",
        "stock_value": 500_000.0,
        "total_asset": 1_500_000.0,
        "pnl_amount": 12_000.0,
        "holdings_count": 1,
        "source": "KIS",
    }


def test_parse_asset_snapshot_response_falls_back_to_cash_when_buyable_missing():
    result = status_logic.parse_asset_snapshot_response({
        "output1": [],
        "output2": [{
            "dnca_tot_amt": "1,000,000",
            "scts_evlu_amt": "500000",
            "tot_evlu_amt": "1500000",
            "evlu_pfls_smtl_amt": "12000",
        }],
    })

    assert result["cash"] == 1_000_000.0
    assert result["buyable_cash"] == 1_000_000.0
    assert result["buyable_cash_source"] == "dnca_tot_amt"


def test_parse_asset_snapshot_response_rejects_kis_error():
    try:
        status_logic.parse_asset_snapshot_response({
            "rt_cd": "1",
            "msg_cd": "EGW00123",
            "msg1": "token expired",
        })
    except RuntimeError as exc:
        assert "KIS balance error" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_parse_asset_snapshot_response_rejects_missing_output2():
    try:
        status_logic.parse_asset_snapshot_response({"rt_cd": "0", "output1": []})
    except RuntimeError as exc:
        assert "missing output2" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_parse_asset_snapshot_response_rejects_invalid_number():
    try:
        status_logic.parse_asset_snapshot_response({
            "rt_cd": "0",
            "output1": [],
            "output2": [{
                "dnca_tot_amt": "not-a-number",
                "ord_psbl_cash": "800000",
                "scts_evlu_amt": "500000",
                "tot_evlu_amt": "1500000",
                "evlu_pfls_smtl_amt": "12000",
            }],
        })
    except RuntimeError as exc:
        assert "invalid field dnca_tot_amt" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")
