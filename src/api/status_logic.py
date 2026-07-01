"""Pure status/F1 display helpers for the API and tests."""

from src.modules.f1_filter import GAP_MAX, GAP_MIN, select_liquidity_candidates


def pipeline_from_logs(logs: list[dict], position_status: str) -> dict:
    stage = 0
    failed = False

    if position_status == "ENTERING":
        return {"pipeline_stage": 2, "pipeline_failed": False}
    if position_status == "HOLDING":
        return {"pipeline_stage": 3, "pipeline_failed": False}
    if position_status == "CLOSED":
        return {"pipeline_stage": 4, "pipeline_failed": False}

    for entry in logs:
        event = entry.get("event")
        if event in {"F1_DONE", "F1_FILTER_EMPTY", "F1_SNAPSHOT_SAVED", "NO_TARGET"}:
            stage = max(stage, 1)
        elif event in {"TARGET_LOCKED", "F2_SKIPPED"}:
            stage = max(stage, 2)
        elif event in {
            "F3_RECHECK",
            "ENTRY_ORDER_SENT",
            "ENTRY_FILL_POLL_TIMEOUT",
            "ENTRY_CANCEL_SENT",
            "ENTRY_RETRY_START",
            "ENTRY_RETRY_SKIPPED",
            "ENTRY_FAIL",
            "F3_SKIPPED",
            "F3_ENTRY_BLOCKED",
            "GAP_CHANGED",
        }:
            stage = max(stage, 2)
        elif event in {"ENTRY_EXECUTED", "DRY_RUN_ENTRY_EXECUTED"}:
            stage = max(stage, 3)
            failed = False

        if event in {"ENTRY_FAIL", "F3_SKIPPED", "F3_ENTRY_BLOCKED", "GAP_CHANGED"}:
            failed = True

    return {"pipeline_stage": stage, "pipeline_failed": failed}


def f1_verdict(candidate: dict) -> str:
    reason = candidate.get("gap_reason")
    labels = {
        "CORE_GAP": "통과",
        "HIGH_GAP_ALLOWED": "고갭통과",
        "GAP_BELOW_2": "갭미달",
        "GAP_BELOW_CORE": "약한갭",
        "HIGH_GAP_AMOUNT_LOW": "대금부족",
        "HIGH_GAP_VI_UNKNOWN": "VI미확인",
        "HIGH_GAP_VI_NEAR": "VI근접",
        "EXTREME_GAP_RISK": "초고갭",
        "GAP_TOO_HIGH": "갭과열",
        "NEGATIVE_GAP": "음수갭",
    }
    return labels.get(str(reason), "통과" if candidate.get("gap_allowed") else "제외")


def f1_allowed(candidate: dict) -> bool:
    if "gap_allowed" in candidate:
        return candidate.get("gap_allowed") is True
    gap = float(candidate.get("gap_pct") or 0)
    return GAP_MIN <= gap < GAP_MAX


def candidate_amount(candidate: dict) -> float:
    return float(candidate.get("expected_amount") or candidate.get("avg_amount_5d") or 0)


def sort_f1_candidates_for_display(rows: list[dict], selected: list[dict]) -> list[dict]:
    selected_tickers = {c.get("ticker") for c in selected}

    def key(candidate: dict) -> tuple:
        ticker = candidate.get("ticker")
        expected_gap = float(candidate.get("expected_api_gap_pct") or 0)
        ranking_gap = float(candidate.get("ranking_gap_pct") or 0)
        expected_valid = (
            float(candidate.get("expected_api_price") or 0) > 0
            and int(candidate.get("expected_api_qty") or 0) > 0
        )
        return (
            ticker in selected_tickers,
            f1_allowed(candidate),
            GAP_MIN <= expected_gap < GAP_MAX,
            GAP_MIN <= ranking_gap < GAP_MAX,
            expected_valid,
            candidate_amount(candidate),
            float(candidate.get("gap_pct") or 0),
        )

    return sorted(rows, key=key, reverse=True)


def f1_summary_from_rows(rows: list[dict]) -> dict:
    gap_pass = [c for c in rows if f1_allowed(c)]
    selected = select_liquidity_candidates(gap_pass)
    display_rows = sort_f1_candidates_for_display(rows, selected)

    expected_valid = [
        c for c in rows
        if float(c.get("expected_api_price") or 0) > 0
        and int(c.get("expected_api_qty") or 0) > 0
    ]
    ranking_pass = [
        c for c in rows
        if GAP_MIN <= float(c.get("ranking_gap_pct") or 0) < GAP_MAX
    ]
    expected_pass = [
        c for c in rows
        if GAP_MIN <= float(c.get("expected_api_gap_pct") or 0) < GAP_MAX
    ]

    return {
        "raw_count": len(rows),
        "expected_valid": len(expected_valid),
        "ranking_pass": len(ranking_pass),
        "expected_pass": len(expected_pass),
        "gap_pass": len(gap_pass),
        "core_gap": sum(1 for c in rows if c.get("gap_band") == "CORE_GAP"),
        "high_gap_allowed": sum(1 for c in rows if c.get("gap_reason") == "HIGH_GAP_ALLOWED"),
        "high_gap_rejected": sum(
            1
            for c in rows
            if c.get("gap_band") == "HIGH_GAP" and c.get("gap_allowed") is not True
        ),
        "extreme_gap": sum(1 for c in rows if c.get("gap_band") == "EXTREME_GAP"),
        "liquidity_pass": len(selected),
        "selected": selected[0] if selected else None,
        "candidates": [
            {**c, "verdict": f1_verdict(c)}
            for c in display_rows[:50]
        ],
    }
