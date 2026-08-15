"""기동 시 canonical 기준선 등록 테스트."""
import json

import pytest

from src import db, release
from src.modules import baseline_experiment as be
from src.modules import cost_model


@pytest.fixture
async def mem():
    await db.init(":memory:")
    be._active_experiment_id = None
    yield
    be._active_experiment_id = None
    await db.close()


async def test_registers_current_fingerprint_and_exact_hypothesis(mem):
    exp_id = await be.ensure_baseline_experiment()
    fp = release.strategy_fingerprint()
    assert exp_id == f"baseline-{fp}"

    row = await db.get_experiment(exp_id)
    assert row["status"] == "ACTIVE"
    assert row["strategy_fingerprint"] == fp
    assert "1.5" in row["hypothesis"] and "2.0" in row["hypothesis"]
    assert row["learn_start_date"] and len(row["learn_start_date"]) == 8


async def test_baseline_config_embeds_cost_assumptions_and_exact_values(mem):
    await be.ensure_baseline_experiment()
    row = await db.get_experiment(f"baseline-{release.strategy_fingerprint()}")
    cfg = await db.get_strategy_config(row["baseline_config_id"])
    config = json.loads(cfg["config_json"])
    assert config["f1_gap_min"] == 0.025
    assert config["step_trail"] == 0.020
    assert config["hard_stop_ratio"] == 0.020
    assert config["first_step"] == 0.025
    assert config["f5_exec"] == "15:15"
    assert config["force_trailing"] == "15:05"
    assert config["kis_concurrency"] == 1
    assert config["cost_assumptions"] == cost_model.cost_assumptions()


async def test_no_secrets_in_config(mem, monkeypatch):
    monkeypatch.setenv("KIS_APP_SECRET", "super-secret-value")
    monkeypatch.setenv("KIS_ACCOUNT_NO", "12345678-01")
    await be.ensure_baseline_experiment()
    row = await db.get_experiment(f"baseline-{release.strategy_fingerprint()}")
    cfg = await db.get_strategy_config(row["baseline_config_id"])
    blob = cfg["config_json"] + (row["strategy_fingerprint"] or "")
    assert "super-secret-value" not in blob
    assert "12345678" not in blob


async def test_idempotent_and_single_current_fingerprint(mem):
    a = await be.ensure_baseline_experiment()
    b = await be.ensure_baseline_experiment()
    assert a == b
    conn = db.get()
    async with conn.execute(
        "SELECT DISTINCT strategy_fingerprint FROM experiment_registry"
    ) as cur:
        fps = {r["strategy_fingerprint"] for r in await cur.fetchall()}
    assert fps == {release.strategy_fingerprint()}


async def test_active_experiment_id_cached(mem):
    exp_id = await be.ensure_baseline_experiment()
    assert be.active_experiment_id() == exp_id


async def test_config_includes_exact_rate_interval(mem):
    from src.api import kis_rest

    await be.ensure_baseline_experiment()
    row = await db.get_experiment(f"baseline-{release.strategy_fingerprint()}")
    cfg = json.loads((await db.get_strategy_config(row["baseline_config_id"]))["config_json"])
    assert cfg["kis_rate_interval_sec"] == kis_rest._RATE_INTERVAL


async def test_new_fingerprint_makes_new_config_with_correct_code_fingerprint(mem, monkeypatch):
    monkeypatch.setattr(be.release, "strategy_fingerprint", lambda: "fp-AAAA")
    be._active_experiment_id = None
    exp_a = await be.ensure_baseline_experiment()
    cfg_a = (await db.get_experiment(exp_a))["baseline_config_id"]
    stored_a = await db.get_strategy_config(cfg_a)

    monkeypatch.setattr(be.release, "strategy_fingerprint", lambda: "fp-BBBB")
    be._active_experiment_id = None
    exp_b = await be.ensure_baseline_experiment()
    cfg_b = (await db.get_experiment(exp_b))["baseline_config_id"]
    stored_b = await db.get_strategy_config(cfg_b)

    # 같은 값이라도 지문이 다르면 새 config_id, 저장된 code_fingerprint도 정확하다.
    assert exp_a != exp_b
    assert cfg_a != cfg_b
    assert stored_a["code_fingerprint"] == "fp-AAAA"
    assert stored_b["code_fingerprint"] == "fp-BBBB"


async def test_open_trade_records_experiment_id(mem):
    exp_id = await be.ensure_baseline_experiment()
    trade_id = await db.open_trade("20260813", "005930", 10_000.0, 10)
    conn = db.get()
    async with conn.execute(
        "SELECT experiment_id FROM trades WHERE id=?", (trade_id,)
    ) as cur:
        row = await cur.fetchone()
    assert row["experiment_id"] == exp_id
