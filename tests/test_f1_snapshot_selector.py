"""공통 최초 정상 F1 스냅샷 선택기 + 원자적 완료 증거 테스트."""
import json

from src.modules import f1_snapshot_selector as sel

ROW = {
    "ticker": "005930",
    "expected_price": 10_300,
    "prev_close": 10_000,
    "gap_pct": 0.03,
    "expected_amount": 200_000_000,
    "vi_gap": 0.02,
    "gap_allowed": True,
    "gap_band": "core",
}


def _row(ticker: str, **over) -> dict:
    r = dict(ROW)
    r["ticker"] = ticker
    r.update(over)
    return r


def _write_snapshot(path, rows):
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )


# ── 완료 증거(사이드카) ────────────────────────────────────────────────

def test_write_completion_sidecar_after_full_write(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A"), _row("B")])
    sc = sel.write_completion_sidecar(p)
    data = json.loads(sc.read_text(encoding="utf-8"))
    assert data["count"] == 2
    assert data["sha256"] == sel.snapshot_hash(p)
    assert data["selector_version"] == sel.SELECTOR_VERSION
    assert data["completed_at"].endswith("+09:00")


def test_sidecar_hash_mismatch_is_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    sel.write_completion_sidecar(p)
    # File mutated after sidecar was written → tampering / partial rewrite.
    _write_snapshot(p, [_row("A"), _row("B")])
    ok, reason = sel.completion_evidence(p)
    assert ok is False
    assert reason == "HASH_MISMATCH"


def _tamper_sidecar(path, **over):
    sc = sel.sidecar_path(path)
    data = json.loads(sc.read_text(encoding="utf-8"))
    data.update(over)
    sc.write_text(json.dumps(data), encoding="utf-8")


def test_sidecar_count_mismatch_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A"), _row("B")])
    sel.write_completion_sidecar(p)
    _tamper_sidecar(p, count=99)
    ok, reason = sel.completion_evidence(p)
    assert ok is False and reason == "SIDECAR_INVALID"


def test_sidecar_path_mismatch_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    sel.write_completion_sidecar(p)
    _tamper_sidecar(p, snapshot_path="somethingelse.jsonl")
    ok, reason = sel.completion_evidence(p)
    assert ok is False and reason == "SIDECAR_INVALID"


def test_sidecar_unknown_version_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    sel.write_completion_sidecar(p)
    _tamper_sidecar(p, selector_version="OLD-0")
    ok, reason = sel.completion_evidence(p)
    assert ok is False and reason == "SIDECAR_INVALID"


def test_sidecar_from_earlier_known_version_still_accepted(tmp_path, monkeypatch):
    """판정 규칙 버전을 올려도 과거 완료 증거는 살아 있어야 한다.

    사이드카는 "쓰기가 끝까지 성공했다"만 증명한다. 정확 일치를 요구하면 버전을
    한 번 올릴 때 분석 가능 거래일이 전부 0으로 떨어진다.
    """
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    sel.write_completion_sidecar(p)  # 현재 버전으로 기록

    monkeypatch.setattr(sel, "SELECTOR_VERSION", "f1-selector-2")
    monkeypatch.setattr(
        sel, "KNOWN_SELECTOR_VERSIONS", frozenset({"f1-selector-1", "f1-selector-2"})
    )
    ok, reason = sel.completion_evidence(p)
    assert ok is True and reason is None


def test_sidecar_naive_completed_at_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    sel.write_completion_sidecar(p)
    _tamper_sidecar(p, completed_at="2026-08-11T09:00:00")  # 오프셋 없음
    ok, reason = sel.completion_evidence(p)
    assert ok is False and reason == "SIDECAR_INVALID"


def test_sidecar_corrupt_json_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    sc = sel.sidecar_path(p)
    sc.write_text("{not json", encoding="utf-8")
    ok, reason = sel.completion_evidence(p)
    assert ok is False and reason == "SIDECAR_INVALID"


def test_missing_evidence_without_sidecar_or_log(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    ok, reason = sel.completion_evidence(p, log_dir=tmp_path)
    assert ok is False
    assert reason == "NO_COMPLETION_EVIDENCE"


# ── 로그 폴백(결정론적 매칭) ────────────────────────────────────────────

def _log_line(event, ts, **fields):
    return json.dumps({"event": event, "ts": ts, **fields}, ensure_ascii=False)


def test_log_fallback_saved_then_done(tmp_path):
    p = tmp_path / "20260811_090005.jsonl"
    _write_snapshot(p, [_row("A")])
    log = tmp_path / "20260811.jsonl"
    log.write_text(
        "\n".join([
            _log_line("F1_SNAPSHOT_SAVED", "2026-08-11T09:00:05+09:00", path=str(p), count=1),
            _log_line("F1_DONE", "2026-08-11T09:00:06+09:00", total_candidates=1, passed=1),
        ]) + "\n",
        encoding="utf-8",
    )
    ok, reason = sel.completion_evidence(p, log_dir=tmp_path)
    assert ok is True
    assert reason is None


def test_log_fallback_intervening_saved_breaks_match(tmp_path):
    p = tmp_path / "20260811_090005.jsonl"
    other = tmp_path / "20260811_090007.jsonl"
    _write_snapshot(p, [_row("A")])
    log = tmp_path / "20260811.jsonl"
    log.write_text(
        "\n".join([
            _log_line("F1_SNAPSHOT_SAVED", "2026-08-11T09:00:05+09:00", path=str(p), count=1),
            _log_line("F1_SNAPSHOT_SAVED", "2026-08-11T09:00:07+09:00", path=str(other), count=1),
            _log_line("F1_DONE", "2026-08-11T09:00:08+09:00", total_candidates=1, passed=1),
        ]) + "\n",
        encoding="utf-8",
    )
    # p's SAVED is followed by another SAVED before F1_DONE → not proven complete.
    ok, reason = sel.completion_evidence(p, log_dir=tmp_path)
    assert ok is False
    assert reason == "NO_COMPLETION_EVIDENCE"


# ── 선택기 규칙 ─────────────────────────────────────────────────────────

def _complete(path):
    sel.write_completion_sidecar(path)


def test_selects_earliest_normal_per_date(tmp_path):
    early = tmp_path / "20260811_090000.jsonl"
    late = tmp_path / "20260811_090500.jsonl"
    for p in (early, late):
        _write_snapshot(p, [_row("A"), _row("B"), _row("C")])
        _complete(p)
    selected, report = sel.select_earliest_normal_snapshots(
        [late, early], market_closed_dates=set(), min_coverage=2
    )
    assert selected == {"20260811": early}
    assert report["reasons"]["DUPLICATE_DATE"] == 1
    assert report["selector_version"] == sel.SELECTOR_VERSION
    assert report["selected"][0]["hash"] == sel.snapshot_hash(early)


def test_outside_window_rejected(tmp_path):
    p = tmp_path / "20260811_091200.jsonl"
    _write_snapshot(p, [_row("A"), _row("B")])
    _complete(p)
    selected, report = sel.select_earliest_normal_snapshots(
        [p], market_closed_dates=set(), min_coverage=1
    )
    assert selected == {}
    assert report["reasons"]["OUTSIDE_WINDOW"] == 1


def test_capture_window_boundary_is_091059_inclusive(tmp_path):
    """계획 §2의 창은 09:00:00~09:10:59다 — 09:11:00은 밖이다."""
    inside = tmp_path / "20260811_091059.jsonl"
    outside = tmp_path / "20260811_091100.jsonl"
    for p in (inside, outside):
        _write_snapshot(p, [_row("A"), _row("B")])
        _complete(p)

    selected, report = sel.select_earliest_normal_snapshots(
        [inside, outside], market_closed_dates=set(), min_coverage=1
    )
    assert selected == {"20260811": inside}
    assert report["reasons"]["OUTSIDE_WINDOW"] == 1

    start = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(start, [_row(f"{i:06d}") for i in range(sel.MIN_UNIVERSE_COVERAGE)])
    _complete(start)
    assert sel.evaluate_snapshot(start, market_closed_dates=set())["reason"] is None


def test_min_coverage_defaults_to_fixed_code_rule(tmp_path):
    """호출자가 값을 넘기지 않아도 코드 상수로 판정이 고정된다."""
    thin = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(thin, [_row(f"{i:06d}") for i in range(sel.MIN_UNIVERSE_COVERAGE - 1)])
    _complete(thin)

    _, report = sel.select_earliest_normal_snapshots(
        [thin], market_closed_dates=set()
    )
    assert report["reasons"]["BELOW_MIN_COVERAGE"] == 1
    assert report["min_coverage"] == sel.MIN_UNIVERSE_COVERAGE


def test_weekend_rejected(tmp_path):
    p = tmp_path / "20260815_090000.jsonl"  # 2026-08-15 Saturday
    _write_snapshot(p, [_row("A")])
    _complete(p)
    _, report = sel.select_earliest_normal_snapshots(
        [p], market_closed_dates=set(), min_coverage=1
    )
    assert report["reasons"]["WEEKEND"] == 1


def test_market_closed_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    _complete(p)
    _, report = sel.select_earliest_normal_snapshots(
        [p], market_closed_dates={"20260811"}, min_coverage=1
    )
    assert report["reasons"]["MARKET_CLOSED"] == 1


def test_missing_required_field_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    bad = _row("A")
    del bad["gap_pct"]
    _write_snapshot(p, [bad])
    _complete(p)
    _, report = sel.select_earliest_normal_snapshots(
        [p], market_closed_dates=set(), min_coverage=1
    )
    assert report["reasons"]["MISSING_REQUIRED_FIELD"] == 1


def test_duplicate_ticker_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A"), _row("A")])
    _complete(p)
    _, report = sel.select_earliest_normal_snapshots(
        [p], market_closed_dates=set(), min_coverage=1
    )
    assert report["reasons"]["DUPLICATE_TICKER"] == 1


def test_below_min_coverage_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A")])
    _complete(p)
    _, report = sel.select_earliest_normal_snapshots(
        [p], market_closed_dates=set(), min_coverage=5
    )
    assert report["reasons"]["BELOW_MIN_COVERAGE"] == 1


def test_invalid_json_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    p.write_text('{"ticker": "A"\nBROKEN\n', encoding="utf-8")
    _complete(p)
    _, report = sel.select_earliest_normal_snapshots(
        [p], market_closed_dates=set(), min_coverage=1
    )
    assert report["reasons"]["INVALID_JSON"] == 1


def test_no_completion_evidence_rejected(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A"), _row("B")])
    # no sidecar, no log
    _, report = sel.select_earliest_normal_snapshots(
        [p], market_closed_dates=set(), min_coverage=1, log_dir=tmp_path
    )
    assert report["reasons"]["NO_COMPLETION_EVIDENCE"] == 1


def test_deterministic_repeated_runs(tmp_path):
    p = tmp_path / "20260811_090000.jsonl"
    _write_snapshot(p, [_row("A"), _row("B")])
    _complete(p)
    r1 = sel.select_earliest_normal_snapshots([p], market_closed_dates=set(), min_coverage=1)
    r2 = sel.select_earliest_normal_snapshots([p], market_closed_dates=set(), min_coverage=1)
    assert r1[0] == r2[0]
    assert r1[1]["reasons"] == r2[1]["reasons"]
