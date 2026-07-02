import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")

import src.api.server as server
from src import db
import src.modules.f1_filter as f1_filter


def test_server_uses_f1_snapshot_dir_constant():
    assert server._F1_SNAPSHOT_DIR == Path(f1_filter.F1_SNAPSHOT_DIR)


def test_f1_snapshot_saved_is_only_weak_done_signal():
    logs = [
        {"event": "F1_SNAPSHOT_SAVED"},
        {"event": "F1_RETRY_WAIT"},
    ]

    status, last_event = server._f1_status_from_logs(logs)

    assert status == "RETRYING"
    assert last_event == logs[-1]


def test_selection_process_summarizes_f1_f2_f3():
    summary = {
        "selected": {"ticker": "006340", "name": "대원전선", "gap_pct": 0.0349, "expected_amount": 147_000_000},
        "liquidity_pass": 10,
        "gap_pass": 12,
        "candidates": [{"ticker": "006340"}, {"ticker": "036930"}],
    }
    logs = [
        {"event": "TARGET_LOCKED", "ticker": "006340", "target_tickers": ["006340", "036930"], "gap_pct": 3.49},
        {"event": "F3_FINAL_PICK", "ticker": "006340", "checked_count": 2, "valid_count": 1, "expected_price": 10670},
    ]

    result = server._selection_process_from_logs(summary, logs)

    assert [row["phase"] for row in result] == ["F1 선정", "F2 선정", "F3 최종"]
    assert result[0]["status"] == "완료"
    assert result[1]["status"] == "잠금"
    assert result[1]["detail"] == "006340, 036930"
    assert result[2]["status"] == "최종"
    assert result[2]["detail"] == "1 / 2 재검증"


def test_selection_process_ignores_f2_from_different_f1_snapshot():
    summary = {
        "selected": {"ticker": "028050", "gap_pct": 0.0651, "expected_amount": 23_901_000_000},
        "liquidity_pass": 4,
        "gap_pass": 4,
        "candidates": [{"ticker": "028050"}, {"ticker": "073240"}],
    }
    logs = [
        {"event": "TARGET_LOCKED", "ticker": "006340", "target_tickers": ["006340"], "gap_pct": 3.49},
        {"event": "F3_FINAL_PICK", "ticker": "006340", "checked_count": 1, "valid_count": 1},
    ]

    result = server._selection_process_from_logs(summary, logs)

    assert result[0]["ticker"] == "028050"
    assert result[1]["ticker"] is None
    assert result[2]["ticker"] is None


@pytest.mark.asyncio
async def test_status_reads_only_recent_logs(monkeypatch):
    limits = []

    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: limits.append(limit) or [])

    await server.api_status()

    assert limits == [server._STATUS_LOG_LIMIT]
    assert server._STATUS_LOG_LIMIT == 50


@pytest.mark.asyncio
async def test_status_includes_asset_snapshot(monkeypatch):
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(
        server,
        "_ASSET_CACHE",
        {
            "cash": 1_000_000.0,
            "buyable_cash": 900_000.0,
            "stock_value": 500_000.0,
            "total_asset": 1_500_000.0,
            "pnl_amount": 12_000.0,
            "holdings_count": 1,
            "source": "KIS",
        },
    )

    resp = await server.api_status()
    body = resp.body.decode("utf-8")

    assert '"assets"' in body
    assert '"cash":1000000.0' in body


@pytest.mark.asyncio
async def test_status_does_not_fetch_asset_snapshot(monkeypatch):
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    fetch = AsyncMock(return_value={"cash": 1.0})
    monkeypatch.setattr(server, "_asset_snapshot_safe", fetch)

    await server.api_status()

    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_status_includes_tick_history_only_while_holding(monkeypatch):
    s = server.state.get()
    server.live.clear_tick_history()
    server.live.push_tick(75_000.0, ticker="005930")
    server.live.push_tick(75_500.0, ticker="005930")
    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: [])
    monkeypatch.setattr(s, "position_status", "HOLDING")
    monkeypatch.setattr(s, "target_ticker", "005930")
    monkeypatch.setattr(s, "entry_price", 75_000.0)
    monkeypatch.setattr(s, "entry_qty", 1)
    monkeypatch.setattr(s, "remaining_qty", 1)
    monkeypatch.setattr(s, "high_price", 75_500.0)

    resp = await server.api_status()
    body = resp.body.decode("utf-8")

    assert '"tick_history"' in body
    assert '"price":75500.0' in body

    monkeypatch.setattr(s, "position_status", "IDLE")
    resp = await server.api_status()
    assert '"tick_history":[]' in resp.body.decode("utf-8")

    server.live.clear_tick_history()


@pytest.mark.asyncio
async def test_assets_refresh_fetches_asset_snapshot(monkeypatch):
    fetch = AsyncMock(return_value={"cash": 1_000_000.0})
    monkeypatch.setattr(server, "_asset_snapshot_safe", fetch)

    resp = await server.api_assets(refresh=1)
    body = resp.body.decode("utf-8")

    fetch.assert_awaited_once()
    assert '"cash":1000000.0' in body


@pytest.mark.asyncio
async def test_api_orders_returns_today_orders(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "orders.db"))
    today = "20260702"
    monkeypatch.setattr(server, "_today", lambda: today)
    trade_id = await db.open_trade(today, "005930", 75_000.0, 10)
    order_id = await db.record_order(trade_id, "ORD001", "BUY", 10, 75_000.0, "FIRST_BUY", "005930")
    await db.update_order_fill(order_id, 75_100.0, 10, 120)
    old_trade_id = await db.open_trade("20260701", "000660", 120_000.0, 1)
    await db.record_order(old_trade_id, "OLD001", "BUY", 1, 120_000.0, "FIRST_BUY", "000660")

    resp = await server.api_orders()
    body = resp.body.decode("utf-8")

    assert '"kis_order_id":"ORD001"' in body
    assert '"order_phase":"FIRST_BUY"' in body
    assert '"status":"FILLED"' in body
    assert "OLD001" not in body
    await db.close()


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_parses_kis_balance(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {
            "output1": [
                {"pdno": "005930", "hldg_qty": "2"},
                {"pdno": "000660", "hldg_qty": "0"},
            ],
            "output2": [{
                "dnca_tot_amt": "1000000",
                "ord_psbl_cash": "800000",
                "scts_evlu_amt": "500000",
                "tot_evlu_amt": "1500000",
                "evlu_pfls_smtl_amt": "12000",
            }],
        }

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    result = await server._fetch_asset_snapshot()

    assert result.items() >= {
        "cash": 1_000_000.0,
        "buyable_cash": 800_000.0,
        "buyable_cash_source": "ord_psbl_cash",
        "stock_value": 500_000.0,
        "total_asset": 1_500_000.0,
        "pnl_amount": 12_000.0,
        "holdings_count": 1,
        "source": "KIS",
        "snapshot_source": "KIS",
    }.items()
    assert "captured_at" in result


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_saves_to_db(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "assets.db"))

    async def fake_get(*args, **kwargs):
        return {
            "rt_cd": "0",
            "output1": [{"pdno": "005930", "hldg_qty": "2"}],
            "output2": [{
                "dnca_tot_amt": "1000000",
                "ord_psbl_cash": "800000",
                "scts_evlu_amt": "500000",
                "tot_evlu_amt": "1500000",
                "evlu_pfls_smtl_amt": "12000",
            }],
        }

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    result = await server._fetch_asset_snapshot()
    conn = db.get()
    async with conn.execute("SELECT total_asset, raw_json FROM asset_snapshots") as cur:
        row = await cur.fetchone()

    assert result["asset_snapshot_id"] > 0
    assert "captured_at" in result
    assert row["total_asset"] == pytest.approx(1_500_000.0)
    assert '"rt_cd":"0"' in row["raw_json"]
    await db.close()


@pytest.mark.asyncio
async def test_assets_without_cache_falls_back_to_latest_db_snapshot(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "assets.db"))
    await db.record_asset_snapshot({"total_asset": 2_000_000.0, "cash": 300_000.0, "source": "KIS"})
    monkeypatch.setattr(server, "_ASSET_CACHE", None)

    resp = await server.api_assets(refresh=0)
    body = resp.body.decode("utf-8")

    assert '"total_asset":2000000.0' in body
    assert '"snapshot_source":"DB"' in body
    assert '"captured_at"' in body
    await db.close()


@pytest.mark.asyncio
async def test_status_without_cache_falls_back_to_latest_db_snapshot(tmp_path, monkeypatch):
    await db.init(str(tmp_path / "assets.db"))
    await db.record_asset_snapshot({"total_asset": 2_000_000.0, "cash": 300_000.0, "source": "KIS"})
    monkeypatch.setattr(server, "_ASSET_CACHE", None)

    resp = await server.api_status()
    body = resp.body.decode("utf-8")

    assert '"total_asset":2000000.0' in body
    assert '"snapshot_source":"DB"' in body
    assert '"captured_at"' in body
    await db.close()


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_rejects_kis_error_response(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {"rt_cd": "1", "msg_cd": "EGW00123", "msg1": "token expired"}

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    with pytest.raises(RuntimeError, match="KIS balance error"):
        await server._fetch_asset_snapshot()


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_rejects_missing_balance_summary(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {"rt_cd": "0", "output1": []}

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    with pytest.raises(RuntimeError, match="missing output2"):
        await server._fetch_asset_snapshot()


@pytest.mark.asyncio
async def test_fetch_asset_snapshot_rejects_invalid_balance_number(monkeypatch):
    async def fake_get(*args, **kwargs):
        return {
            "rt_cd": "0",
            "output1": [],
            "output2": [{
                "dnca_tot_amt": "not-a-number",
                "ord_psbl_cash": "800000",
                "scts_evlu_amt": "500000",
                "tot_evlu_amt": "1500000",
                "evlu_pfls_smtl_amt": "12000",
            }],
        }

    monkeypatch.setattr(server.kis_rest, "get", fake_get)

    with pytest.raises(RuntimeError, match="invalid field dnca_tot_amt"):
        await server._fetch_asset_snapshot()


@pytest.mark.asyncio
async def test_asset_snapshot_safe_first_load_waits_for_inflight_refresh(monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def fake_fetch():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"cash": 7.0}

    monkeypatch.setattr(server, "_ASSET_CACHE", None)
    monkeypatch.setattr(server, "_ASSET_CACHE_AT", 0.0)
    monkeypatch.setattr(server, "_ASSET_CACHE_LOCK", asyncio.Lock())
    monkeypatch.setattr(server, "_fetch_asset_snapshot", fake_fetch)

    first = asyncio.create_task(server._asset_snapshot_safe())
    await started.wait()
    second = asyncio.create_task(server._asset_snapshot_safe())
    await asyncio.sleep(0)

    assert second.done() is False

    release.set()
    assert await asyncio.gather(first, second) == [{"cash": 7.0}, {"cash": 7.0}]
    assert calls == 1


@pytest.mark.asyncio
async def test_asset_snapshot_safe_records_failure_reason(monkeypatch):
    events = []

    async def fake_fetch():
        raise RuntimeError("KIS balance error rt_cd=1 msg_cd=EGW00123 msg1=token expired")

    monkeypatch.setattr(server, "_ASSET_CACHE", None)
    monkeypatch.setattr(server, "_ASSET_CACHE_AT", 0.0)
    monkeypatch.setattr(server, "_ASSET_LAST_ERROR", None)
    monkeypatch.setattr(server, "_ASSET_CACHE_LOCK", asyncio.Lock())
    monkeypatch.setattr(server, "_fetch_asset_snapshot", fake_fetch)
    monkeypatch.setattr(server, "log", lambda event, **kwargs: events.append((event, kwargs)))

    resp = await server.api_assets(refresh=1)
    body = resp.body.decode("utf-8")

    assert '"assets":null' in body
    assert "EGW00123" in body
    assert events[0][0] == "ASSET_SNAPSHOT_FAILED"
    assert events[0][1]["error_type"] == "RuntimeError"
