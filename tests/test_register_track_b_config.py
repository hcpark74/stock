"""확정 규칙의 설정 등록 검증.

config 는 해시로 고정된다. 값이 하나 달라지면 새 config_id 가 나와야 하고,
같으면 재사용돼야 한다 — 그래야 그림자 기록을 나중에 규칙별로 갈라 읽는다.
"""

from scripts.register_track_b_config import build_config


def test_config_carries_rule_key_and_exit_constants():
    config = build_config("R1", {"min_bars_after_gap": 2})
    assert config["b_rule"] == "R1"
    assert config["b_signal_start"] == "09:35"
    assert config["b_entry_deadline"] == "14:00"
    # 청산은 A와 같은 한 벌이다. 설계 §4.1.
    assert config["b_step_size"] == 0.025
    assert config["b_step_trail"] == 0.020
    assert config["b_hard_stop_ratio"] == 0.020


def test_config_is_stable_for_same_inputs():
    assert build_config("R1", {"min_bars_after_gap": 2}) == build_config(
        "R1", {"min_bars_after_gap": 2}
    )


def test_unknown_rule_is_rejected():
    import pytest

    with pytest.raises(ValueError):
        build_config("R9", {})
