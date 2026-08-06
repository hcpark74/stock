"""실전투자 준비도 점수와 100% 게이트 테스트."""

import pytest

from src import db, readiness, release


@pytest.fixture
async def mem(monkeypatch):
    await db.init(":memory:")
    monkeypatch.setenv("KIS_MODE", "PAPER")
    yield
    await db.close()


def _safe_env(monkeypatch):
    values = {
        "F3_ALLOC_RATIO": "0.20",
        "FIRST_RATIO": "1.00",
        "F2_RETRY_F1_ON_FAIL": "0",
        "PAPER_FAST_PROBE": "0",
        "PAPER_FAST_HYBRID": "0",
        "F3_FINAL_QUOTE_MAX_AGE_MS": "500",
        "F4_REST_BACKUP_ENABLED": "1",
        "F4_REST_ONLY_WHEN_WS_STALE": "1",
        "DRY_RUN": "0",
        "FORCE_CATCHUP": "0",
        "REAL_CONFIG_VERIFIED": "1",
        "REAL_SMOKE_TEST_APPROVED": "1",
        "REAL_ALERT_TEST_APPROVED": "1",
        "REAL_OPERATOR_DRILL_APPROVED": "1",
        "REAL_READINESS_MIN_PAPER_TRADES": "20",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


async def test_new_strategy_fingerprint_starts_with_zero_paper_evidence(mem, monkeypatch):
    _safe_env(monkeypatch)

    report = await readiness.calculate()

    assert report["groups"][0]["earned"] == 30
    assert report["groups"][1]["earned"] == 0
    assert report["clean_paper_trades"] == 0
    assert report["percent"] == 70
    assert report["eligible_for_real"] is False


async def test_twenty_clean_same_version_paper_trades_reach_100(mem, monkeypatch):
    _safe_env(monkeypatch)
    for day in range(1, 21):
        date = f"202609{day:02d}"
        trade_id = await db.open_trade(date, "005930", 10_000.0, 10, "삼성전자")
        await db.close_trade(
            trade_id,
            10_100.0,
            "TRAILING",
            1.0,
            0.0,
            exit_qty=10,
            high_price=10_200.0,
        )

    report = await readiness.calculate()

    assert report["clean_paper_trades"] == 20
    assert report["percent"] == 100
    assert report["eligible_for_real"] is True
    assert report["blockers"] == []


async def test_changed_fingerprint_resets_paper_evidence_to_zero(mem, monkeypatch):
    _safe_env(monkeypatch)
    monkeypatch.setattr(release, "strategy_fingerprint", lambda: "old-version")
    for day in range(1, 21):
        trade_id = await db.open_trade(
            f"202610{day:02d}", "005930", 10_000.0, 10, "삼성전자"
        )
        await db.close_trade(
            trade_id,
            10_100.0,
            "TRAILING",
            1.0,
            0.0,
            exit_qty=10,
            high_price=10_200.0,
        )
    monkeypatch.setattr(readiness, "strategy_fingerprint", lambda: "new-version")

    report = await readiness.calculate()

    assert report["strategy_fingerprint"] == "new-version"
    assert report["clean_paper_trades"] == 0
    assert report["groups"][1]["earned"] == 0
    assert report["eligible_for_real"] is False


async def test_first_ratio_env_cannot_disagree_with_runtime_gate(mem, monkeypatch):
    _safe_env(monkeypatch)
    monkeypatch.setenv("FIRST_RATIO", "0.7")

    report = await readiness.calculate()

    check = next(
        check
        for check in report["groups"][2]["checks"]
        if check["key"] == "pyramiding_disabled"
    )
    assert check["passed"] is True
    assert check["detail"] == "runtime FIRST_RATIO=1"
