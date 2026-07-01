from pathlib import Path

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


@pytest.mark.asyncio
async def test_status_reads_only_recent_logs(monkeypatch):
    limits = []

    monkeypatch.setattr(server, "_read_today_logs", lambda limit=None: limits.append(limit) or [])

    await server.api_status()

    assert limits == [server._STATUS_LOG_LIMIT]
    assert server._STATUS_LOG_LIMIT == 50
