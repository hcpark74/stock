import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import bars

KST = ZoneInfo("Asia/Seoul")


def _session(n=381, date=None):
    """실제 세션 모양 — 09:00부터 1분 간격에 단일가 종가 15:30 한 봉.

    같은 시각 봉을 N개 복제하면 개수는 맞아도 세션이 아니다. 완결성 판정이
    개장~마감을 덮었는지를 보므로 시각이 실제와 같아야 한다.
    """
    def bar(time):
        row = {"time": time, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1}
        if date is not None:
            row["date"] = date
        return row

    rows = [bar(f"{9 + i // 60:02d}{i % 60:02d}00") for i in range(n - 1)]
    rows.append(bar("153000"))
    return rows


def _morning(n, date=None):
    """09:00부터 n분치 조각 — 마감에 닿지 않는다."""
    return _session(n + 1, date=date)[:n]


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


async def test_warmup_fetch_is_skipped_when_the_existing_file_is_a_complete_session(
    monkeypatch, tmp_path
):
    """완결된 세션 파일은 이미 확보된 것으로 치고 다시 받지 않는다."""
    complete = _session(300)
    (tmp_path / "20260831_005930.json").write_text(
        json.dumps(complete), encoding="utf-8"
    )
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


async def test_warmup_fetch_refetches_when_the_existing_file_is_partial(
    monkeypatch, tmp_path
):
    """레코더가 09:01에 마감한 종목은 20봉짜리 스텁만 남긴다. 그 스텁을 확보로 치면
    그 (날짜, 종목) 쌍은 영영 데울 수 없다 — 존재만으로 True를 돌려주면 안 된다."""
    stub = _morning(20)
    (tmp_path / "20260831_005930.json").write_text(json.dumps(stub), encoding="utf-8")
    complete_session = _session(381, date="20260831")
    called = []

    async def fake_session(trade_date, ticker, *, max_pages=20):
        called.append((trade_date, ticker))
        return complete_session

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is True
    assert called == [("20260831", "005930")]
    written = json.loads(
        (tmp_path / "20260831_005930.json").read_text(encoding="utf-8")
    )
    assert len(written) == 381


async def test_warmup_fetch_writes_the_previous_session_to_disk(
    monkeypatch, tmp_path
):
    async def fake_session(trade_date, ticker, *, max_pages=20):
        return _session(381, date=trade_date)

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is True
    written = json.loads(
        (tmp_path / "20260831_005930.json").read_text(encoding="utf-8")
    )
    assert len(written) == 381
    assert written[0]["time"] == "090000"


async def test_warmup_fetch_does_not_write_a_truncated_session(monkeypatch, tmp_path):
    """페이지 상한에 걸려 세션이 짧게 끝나면 그 결과를 파일로 남기지 않는다 —
    남기면 다음 호출이 그 조각을 완결로 착각한다(§_is_complete_session).

    조회는 마감 커서에서 역방향으로 밀므로 잘리면 **오후만** 남는다. 아침 조각은
    이 경로가 만들 수 없는 모양이라 잘림을 시험하지 못한다.
    """
    async def fake_session(trade_date, ticker, *, max_pages=20):
        return _session(date=trade_date)[200:]

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is False
    assert not (tmp_path / "20260831_005930.json").exists()


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
        return _session(381, date=trade_date)

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert ok is True
    written = json.loads(
        (missing_dir / "20260831_005930.json").read_text(encoding="utf-8")
    )
    assert len(written) == 381


async def test_warmup_fetch_refetches_a_long_file_that_misses_the_close(
    monkeypatch, tmp_path
):
    """봉이 300개여도 마감에 닿지 않으면 다시 받는다.

    이 자리에는 한때 별도의 개수 문턱(300)이 있었다. 그 문턱으로 되돌리면 이
    파일이 완결로 통과해 다시 받지 않는다 — 그것을 이 테스트가 막는다.
    """
    lunch = _morning(300)                       # 09:00~13:59
    (tmp_path / "20260831_005930.json").write_text(
        json.dumps(lunch), encoding="utf-8"
    )
    called = []

    async def fake_session(trade_date, ticker, *, max_pages=20):
        called.append((trade_date, ticker))
        return _session(date=trade_date)

    monkeypatch.setattr(bars.kis_minute_bars, "fetch_session", fake_session)

    ok = await bars.ensure_warmup(
        "20260901", "005930", "20260831",
        now=datetime(2026, 9, 1, 9, 30, tzinfo=KST),
    )

    assert len(lunch) >= 300
    assert ok is True
    assert called == [("20260831", "005930")]
