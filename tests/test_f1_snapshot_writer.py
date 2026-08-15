"""F1 스냅샷 원자적 쓰기 + 완료 사이드카 테스트."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from src.modules import f1_filter
from src.modules import f1_snapshot_selector as sel

KST = ZoneInfo("Asia/Seoul")

CANDS = [
    {"ticker": "005930", "gap_pct": 0.03},
    {"ticker": "000660", "gap_pct": 0.04},
]


def test_writer_creates_snapshot_and_sidecar(tmp_path):
    now = datetime(2026, 8, 11, 9, 0, 0, tzinfo=KST)
    path = f1_filter.write_candidate_snapshot(tmp_path, CANDS, now)
    assert path.name == "20260811_090000.jsonl"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [r["ticker"] for r in rows] == ["005930", "000660"]
    ok, reason = sel.completion_evidence(path)
    assert ok is True and reason is None


def test_writer_leaves_no_tmp_and_no_partial_sidecar(tmp_path):
    now = datetime(2026, 8, 11, 9, 0, 0, tzinfo=KST)
    f1_filter.write_candidate_snapshot(tmp_path, CANDS, now)
    assert list(tmp_path.glob("*.tmp")) == []


def test_rotation_removes_sidecar_with_snapshot(tmp_path):
    for i in range(3):
        now = datetime(2026, 8, 11, 9, 0, i, tzinfo=KST)
        f1_filter.write_candidate_snapshot(tmp_path, CANDS, now)
    f1_filter._rotate_candidate_snapshots(tmp_path, keep=1)
    remaining = sorted(p.name for p in tmp_path.glob("*.jsonl"))
    sidecars = sorted(p.name for p in tmp_path.glob("*.done.json"))
    assert remaining == ["20260811_090002.jsonl"]
    assert sidecars == ["20260811_090002.jsonl.done.json"]
