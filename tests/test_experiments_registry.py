"""experiment_registry / strategy_configs / price_path_manifests CRUD 테스트."""
import pytest

from src import db


@pytest.fixture
async def mem():
    await db.init(":memory:")
    yield
    await db.close()


# ── strategy_configs ────────────────────────────────────────────────────

async def test_config_same_hash_reuses_id(mem):
    cfg = {"trail": 0.02, "hard_stop": 0.02}
    a = await db.upsert_strategy_config(cfg, kind="PRIMARY", code_fingerprint="fp1")
    b = await db.upsert_strategy_config(
        dict(reversed(list(cfg.items()))), kind="PRIMARY", code_fingerprint="fp1"
    )
    assert a == b  # 정규화 해시가 같으면 동일 config_id 재사용


async def test_config_value_change_makes_new_id(mem):
    a = await db.upsert_strategy_config({"trail": 0.02}, kind="PRIMARY", code_fingerprint="fp1")
    b = await db.upsert_strategy_config({"trail": 0.015}, kind="PRIMARY", code_fingerprint="fp1")
    assert a != b


async def test_config_row_has_offset_timestamp(mem):
    cid = await db.upsert_strategy_config({"x": 1}, kind="PRIMARY", code_fingerprint="fp1")
    conn = db.get()
    async with conn.execute(
        "SELECT created_at, config_sha256, kind FROM strategy_configs WHERE config_id=?",
        (cid,),
    ) as cur:
        row = await cur.fetchone()
    assert row["created_at"].endswith("+09:00")
    assert row["kind"] == "PRIMARY"
    assert len(row["config_sha256"]) == 64


# ── experiment_registry ─────────────────────────────────────────────────

async def _register(status="ACTIVE"):
    return await db.register_experiment(
        "baseline-abc",
        hypothesis="trail 1.5 vs 2.0",
        baseline_config_id="cfg-1",
        candidate_config_id="cfg-2",
        primary_metric="cost-net paired",
        learn_start_date="20260813",
        stop_conditions={"consecutive_losses": 4},
        safety_limits={"single_trade_conservative_net_pct": -3.5},
        strategy_fingerprint="fp-current",
        status=status,
    )


async def test_register_experiment_persists(mem):
    row = await _register()
    assert row["experiment_id"] == "baseline-abc"
    assert row["status"] == "ACTIVE"
    assert row["strategy_fingerprint"] == "fp-current"
    assert row["created_at"].endswith("+09:00")


async def test_register_experiment_is_idempotent(mem):
    await _register()
    # 두 번째 호출이 시작값을 덮어쓰지 않는다.
    again = await db.register_experiment(
        "baseline-abc",
        hypothesis="DIFFERENT",
        baseline_config_id="cfg-9",
        candidate_config_id="cfg-9",
        primary_metric="DIFFERENT",
        learn_start_date="20991231",
        stop_conditions={},
        safety_limits={},
        strategy_fingerprint="fp-other",
        status="RETIRED",
    )
    assert again["hypothesis"] == "trail 1.5 vs 2.0"
    assert again["learn_start_date"] == "20260813"
    assert again["strategy_fingerprint"] == "fp-current"


async def test_get_active_experiment(mem):
    await _register()
    active = await db.get_active_experiment()
    assert active["experiment_id"] == "baseline-abc"


# ── price_path_manifests ────────────────────────────────────────────────

def _manifest(**over):
    base = dict(
        trade_date="20260813",
        ticker="005930",
        trade_id=27,
        experiment_id="baseline-abc",
        chunks_json="[]",
        first_source_ts="2026-08-13T09:10:10+09:00",
        last_source_ts="2026-08-13T15:15:00+09:00",
        first_received_at="2026-08-13T09:10:10+09:00",
        last_received_at="2026-08-13T15:15:00+09:00",
        seq_gaps=0,
        ws_disconnects=0,
        rest_backfill_ranges_json="[]",
        reached_expected_close=1,
        data_complete=1,
        missing_reason=None,
        writer_version="w1",
        schema_version="s1",
        content_hash="hash-a",
    )
    base.update(over)
    return base


async def test_manifest_upsert_idempotent(mem):
    await db.upsert_price_path_manifest(**_manifest())
    await db.upsert_price_path_manifest(**_manifest())
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-abc")
    assert m["data_complete"] == 1
    assert m["created_at"].endswith("+09:00")


async def test_manifest_null_experiment_id_upserts_idempotently(mem):
    # experiment_id 미등록(None) 경로도 sentinel 정규화로 UNIQUE/UPSERT가 동작한다.
    await db.upsert_price_path_manifest(**_manifest(experiment_id=None, content_hash="h1"))
    await db.upsert_price_path_manifest(**_manifest(experiment_id=None, content_hash="h1"))
    conn = db.get()
    async with conn.execute(
        "SELECT COUNT(*) AS n FROM price_path_manifests WHERE ticker='005930'"
    ) as cur:
        row = await cur.fetchone()
    assert row["n"] == 1
    m = await db.get_price_path_manifest("20260813", "005930", None)
    assert m is not None
    assert m["experiment_id"] == ""


async def test_manifest_records_source_ts_reversals_separately(mem):
    await db.upsert_price_path_manifest(**_manifest(seq_gaps=0, source_ts_reversals=3))
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-abc")
    assert m["seq_gaps"] == 0
    assert m["source_ts_reversals"] == 3


async def test_manifest_hash_change_appends_correction_not_overwrite(mem):
    import json

    await db.upsert_price_path_manifest(**_manifest(content_hash="hash-a"))
    await db.upsert_price_path_manifest(**_manifest(content_hash="hash-b"))
    m = await db.get_price_path_manifest("20260813", "005930", "baseline-abc")
    assert m["content_hash"] == "hash-b"
    history = json.loads(m["correction_history_json"])
    assert len(history) == 1
    assert history[0]["prev_content_hash"] == "hash-a"
    assert "at" in history[0]
