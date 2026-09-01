import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import bars

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    yield


async def test_warmup_fetch_is_refused_inside_the_forbidden_window(monkeypatch):
    """09:00~09:11은 A의 진입 창이다. 여기서 분봉 API를 부르지 않는다."""
    called = []

    async def fake_session(trade_date, ticker, *, max_pages=20):
        called.append((trade_date, ticker))
        return []

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 5, tzinfo=KST),
    )

    assert ok is False
    assert called == []


async def test_warmup_fetch_is_skipped_when_the_file_already_exists(
    monkeypatch, tmp_path
):
    (tmp_path / "20260831_005930.json").write_text(json.dumps([
        {"time": "090000", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
    ]), encoding="utf-8")
    called = []

    async def fake_session(trade_date, ticker, *, max_pages=20):
        called.append((trade_date, ticker))
        return []

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is True
    assert called == []


async def test_warmup_fetch_writes_the_previous_session_to_disk(
    monkeypatch, tmp_path
):
    async def fake_session(trade_date, ticker, *, max_pages=20):
        return [{"date": trade_date, "time": "090000", "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1}]

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is True
    written = json.loads(
        (tmp_path / "20260831_005930.json").read_text(encoding="utf-8")
    )
    assert [r["time"] for r in written] == ["090000"]


async def test_warmup_fetch_reports_failure_without_raising(monkeypatch):
    """워밍업 실패가 판정 경로를 막으면 안 된다."""
    async def boom(trade_date, ticker, *, max_pages=20):
        raise RuntimeError("network down")

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", boom)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is False


async def test_warmup_fetch_leaves_no_file_when_the_session_is_empty(
    monkeypatch, tmp_path
):
    """빈 응답을 빈 파일로 남기면 다음 호출이 '이미 있다'고 착각한다."""
    async def empty(trade_date, ticker, *, max_pages=20):
        return []

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", empty)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is False
    assert not (tmp_path / "20260831_005930.json").exists()


async def test_warmup_fetch_creates_the_bars_dir_when_missing(monkeypatch, tmp_path):
    """``_BARS_DIR``가 아직 없어도 쓰기가 raise 하면 안 된다.

    ``isolated`` 픽스처의 ``tmp_path``는 pytest가 미리 만들어 두므로, 이 경우를
    재현하려면 아직 존재하지 않는 하위 디렉터리를 가리키게 해야 한다.
    """
    missing_dir = tmp_path / "nested" / "bars"
    monkeypatch.setattr(bars, "_BARS_DIR", missing_dir)

    async def fake_session(trade_date, ticker, *, max_pages=20):
        return [{"date": trade_date, "time": "090000", "open": 1, "high": 1,
                 "low": 1, "close": 1, "volume": 1}]

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is True
    written = json.loads(
        (missing_dir / "20260831_005930.json").read_text(encoding="utf-8")
    )
    assert [r["time"] for r in written] == ["090000"]
