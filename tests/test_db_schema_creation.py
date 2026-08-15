"""재기동 시 테이블 생성 + finalized 트레일 shadow 보존 회귀 테스트."""
import pytest

from src import db

EXPECTED_TABLES = {
    "trailing_shadow_comparisons",
    "experiment_registry",
    "strategy_configs",
    "price_path_manifests",
}


@pytest.fixture
async def mem():
    await db.init(":memory:")
    yield
    await db.close()


async def _table_names() -> set[str]:
    conn = db.get()
    async with conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cur:
        rows = await cur.fetchall()
    return {row["name"] for row in rows}


async def test_all_phase0_tables_created(mem):
    assert EXPECTED_TABLES <= await _table_names()


async def _finalize_sample_shadow(trade_id: int) -> None:
    await db.record_trailing_shadow_baseline(
        trade_id,
        baseline_step_trail=0.015,
        recommended_step_trail=0.020,
        entry_price=10_000.0,
        highest_step=0.05,
        baseline_stop_price=10_350.0,
        recommended_stop_price=10_300.0,
        baseline_exit_price=10_350.0,
    )
    await db.finalize_trailing_shadow_comparison(
        trade_id,
        baseline_step_trail=0.015,
        recommended_step_trail=0.020,
        entry_price=10_000.0,
        exit_qty=10,
        highest_step=0.05,
        baseline_stop_price=10_350.0,
        recommended_stop_price=10_300.0,
        recommended_exit_price=10_300.0,
        actual_exit_price=10_290.0,
        actual_pnl_pct=2.9,
        close_reason="TRAILING",
    )


async def test_reinit_on_file_is_idempotent_and_preserves_finalized_shadow(tmp_path):
    path = str(tmp_path / "trading.db")
    await db.init(path)
    trade_id = await db.open_trade("20260813", "005930", 10_000.0, 10)
    await _finalize_sample_shadow(trade_id)
    await db.close()

    # 재기동: 같은 파일로 다시 init → CREATE IF NOT EXISTS / ALTER 재적용.
    await db.init(path)
    try:
        assert EXPECTED_TABLES <= await _table_names()
        row = await db.get_trailing_shadow_comparison(trade_id)
        assert row["finalized"] == 1
        assert row["actual_exit_price"] == 10_290.0
        assert row["recommended_step_trail"] == 0.020
    finally:
        await db.close()


async def test_trades_have_experiment_id_column(mem):
    conn = db.get()
    async with conn.execute("PRAGMA table_info(trades)") as cur:
        cols = {row["name"] for row in await cur.fetchall()}
    assert "experiment_id" in cols
