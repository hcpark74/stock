"""테스트 공통 설정."""
import logging
import os
import sys

import pytest

# 테스트 모듈 수집 중 main.py가 개발 머신의 .env를 읽어 모듈 상수를
# 바꾸지 않도록, 어떤 테스트 모듈보다 먼저 dotenv 로드를 비활성화한다.
os.environ["STOCK_SKIP_DOTENV"] = "1"

# 테스트 중 stock 로거 stdout/파일 출력 억제
_stock_log = logging.getLogger("stock")
_stock_log.handlers = []
_stock_log.propagate = False


@pytest.fixture(autouse=True)
def isolate_runtime_state_dir(tmp_path_factory, monkeypatch):
    """Unit tests must never write the live bot's persisted state."""
    state_dir = tmp_path_factory.mktemp("runtime-state")
    monkeypatch.setenv("STATE_DIR", str(state_dir))
    main_module = sys.modules.get("main")
    if main_module is not None:
        monkeypatch.setattr(main_module, "STATE_DIR", str(state_dir))
