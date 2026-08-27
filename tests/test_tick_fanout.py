import pytest

from src.modules import tick_capture


@pytest.fixture(autouse=True)
def clean_listeners():
    tick_capture.clear_tick_listeners()
    yield
    tick_capture.clear_tick_listeners()


def _tick(ticker="006340", price=14570.0):
    return {
        "source_ts": "2026-08-27T09:35:00+09:00",
        "received_at": "2026-08-27T09:35:00.100000+09:00",
        "price": price,
        "qty": 100,
        "source": "ws",
        "valid": True,
        "ticker": ticker,
        "raw": ["006340", "093500", str(price)] + [""] * 43,
    }


def test_listener_receives_the_full_tick_dict():
    seen = []
    tick_capture.register_tick_listener(seen.append)

    tick_capture.enqueue(_tick())

    assert len(seen) == 1
    assert seen[0]["qty"] == 100
    assert len(seen[0]["raw"]) == 46


def test_listener_runs_even_when_no_capture_is_attached():
    # 캡처가 붙지 않은 날에도 트랙 B는 봉을 만들어야 한다.
    assert tick_capture._capture is None
    seen = []
    tick_capture.register_tick_listener(seen.append)

    tick_capture.enqueue(_tick())

    assert len(seen) == 1


def test_a_raising_listener_does_not_break_the_others_or_the_caller(monkeypatch):
    # 이 브랜치에서 가장 중요한 주장이다 — 트랙 B의 예외가 트랙 A의 durable
    # 틱 캡처를 끊으면 안 된다. 그래서 캡처 대역을 붙여 놓고 그 enqueue가
    # 실제로 틱을 받았는지 단언한다(눈으로 보는 검증 대신).
    seen = []
    captured = []

    class _StubCapture:
        ticker = "006340"

        def enqueue(self, tick):
            captured.append(tick)

    monkeypatch.setattr(tick_capture, "_capture", _StubCapture())

    def boom(_tick):
        raise RuntimeError("listener exploded")

    tick_capture.register_tick_listener(boom)
    tick_capture.register_tick_listener(seen.append)

    tick_capture.enqueue(_tick())   # 예외가 새어 나오면 실패

    assert len(seen) == 1
    assert len(captured) == 1
    assert captured[0]["price"] == 14570.0


def test_clear_removes_every_listener():
    seen = []
    tick_capture.register_tick_listener(seen.append)
    tick_capture.clear_tick_listeners()

    tick_capture.enqueue(_tick())

    assert seen == []
