"""테스트 공통 설정."""
import logging

import pytest

# 테스트 중 stock 로거 stdout/파일 출력 억제
_stock_log = logging.getLogger("stock")
_stock_log.handlers = []
_stock_log.propagate = False


@pytest.fixture(autouse=True)
def isolate_runtime_state_dir(tmp_path_factory, monkeypatch):
    """Unit tests must never write the live bot's persisted state."""
    state_dir = tmp_path_factory.mktemp("runtime-state")
    monkeypatch.setenv("STATE_DIR", str(state_dir))
