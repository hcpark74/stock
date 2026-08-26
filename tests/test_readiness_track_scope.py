"""REAL 전환 게이트는 트랙 A의 PAPER 증거만 센다."""

import pytest

from src import db, readiness


@pytest.fixture
async def mem(monkeypatch):
    await db.init(":memory:")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    yield
    await db.close()


async def _closed_paper_trade(date: str, track: str) -> None:
    trade_id = await db.open_trade(date, "005930", 10_000.0, 10, "삼성전자", track)
    await db.close_trade(
        trade_id,
        10_100.0,
        "TRAILING",
        1.0,
        0.0,
        exit_qty=10,
        high_price=10_200.0,
    )


async def test_track_b_paper_trades_do_not_count_toward_real_gate(mem):
    fingerprint = await _fingerprint()

    await _closed_paper_trade("20260901", "A")
    assert await readiness._clean_paper_trade_count(fingerprint) == 1

    # 같은 날 트랙 B가 무결한 PAPER 청산을 하나 더 기록해도
    # A의 실탄 자격 근거는 늘어나지 않는다.
    await _closed_paper_trade("20260901", "B")
    assert await readiness._clean_paper_trade_count(fingerprint) == 1


async def test_only_track_b_evidence_leaves_the_gate_at_zero(mem):
    fingerprint = await _fingerprint()

    for day in range(1, 21):
        await _closed_paper_trade(f"202609{day:02d}", "B")

    assert await readiness._clean_paper_trade_count(fingerprint) == 0


async def _fingerprint() -> str:
    from src.release import strategy_fingerprint

    return strategy_fingerprint()
