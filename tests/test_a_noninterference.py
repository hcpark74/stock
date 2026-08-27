import time

import pytest

from src import bars, release
from src.modules import tick_capture


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(bars, "_BARS_DIR", tmp_path)
    tick_capture.clear_tick_listeners()
    bars.reset()
    yield
    bars.reset()
    tick_capture.clear_tick_listeners()


def _tick(i):
    ts = f"2026-08-27T09:35:{i % 60:02d}+09:00"
    raw = [""] * 46
    raw[0], raw[2] = "006340", str(14500 + (i % 50))
    return {
        "source_ts": ts, "received_at": ts, "price": 14500.0 + (i % 50),
        "qty": 10, "source": "ws", "valid": True, "ticker": "006340", "raw": raw,
    }


def _elapsed(ticks):
    started = time.perf_counter()
    for tick in ticks:
        tick_capture.enqueue(tick)
    return time.perf_counter() - started


def test_track_b_files_are_not_in_the_strategy_fingerprint():
    for name in (
        "src/bars.py",
        "src/indicators.py",
        "src/api/kis_minute_bars.py",
        "src/modules/tick_capture.py",
        "src/api/server.py",
    ):
        assert name not in release._STRATEGY_FILES


def test_the_listener_adds_no_measurable_cost_to_the_tick_path():
    ticks = [_tick(i) for i in range(20_000)]

    baseline = min(_elapsed(ticks) for _ in range(3))
    bars.install()
    with_listener = min(_elapsed(ticks) for _ in range(3))

    # 동기 경로에 더해지는 일은 deque.append 하나다. 3배는 매우 느슨한 상한이고,
    # 여기서 걸린다면 팬아웃 지점에서 집계·지표를 돌리고 있다는 뜻이다.
    assert with_listener < baseline * 3 + 0.05


def test_stage_one_never_opens_a_database_connection():
    # 컨트롤러 판정: db._conn이 None이라는 절대 단언은 이 테스트 파일이
    # test_api_*, test_db_*보다 먼저 실행된다는 pytest 수집 순서에 암묵적으로
    # 의존한다. 이전에 실행된 어떤 테스트가 db.init()을 불렀다면 _conn이 이미
    # 채워져 있고, 이 테스트는 여기서 검증하려는 것과 무관한 이유로 실패한다.
    # 이 테스트가 잠그려는 주장은 "1단계는 DB를 건드리지 않는다"이지
    # "이 테스트보다 먼저 실행된 어떤 테스트도 커넥션을 열지 않았다"가 아니다.
    # 그래서 절대값 대신 틱 폭주 전후의 동일성을 비교한다.
    from src import db

    before = db._conn

    bars.install()
    for i in range(200):
        tick_capture.enqueue(_tick(i))
    bars.drain()

    # 봉 집계가 DB를 건드렸다면 커넥션 객체가 (열렸든 새로 열렸든) 달라졌을 것이다.
    assert db._conn is before


def test_the_bars_module_does_not_import_db():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(bars))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imported.add(f"{node.module}.{alias.name}")

    assert "src.db" not in imported
    assert not any(name.endswith(".db") for name in imported)
