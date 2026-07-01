import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("fastapi")

import src.api.server as server
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
async def test_assets_refresh_fetches_asset_snapshot(monkeypatch):
    fetch = AsyncMock(return_value={"cash": 1_000_000.0})
    monkeypatch.setattr(server, "_asset_snapshot_safe", fetch)

    resp = await server.api_assets(refresh=1)
    body = resp.body.decode("utf-8")

    fetch.assert_awaited_once()
    assert '"cash":1000000.0' in body


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

    assert result == {
        "cash": 1_000_000.0,
        "buyable_cash": 800_000.0,
        "stock_value": 500_000.0,
        "total_asset": 1_500_000.0,
        "pnl_amount": 12_000.0,
        "holdings_count": 1,
        "source": "KIS",
    }


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
