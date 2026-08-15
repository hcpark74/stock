"""비용·슬리피지 모델 순수 계산 단위 테스트."""
import pytest

from src.modules import cost_model as cm


def test_cost_assumptions_exposes_frozen_constants():
    a = cm.cost_assumptions()
    assert a["base_round_trip_cost_pct"] == 0.18
    assert a["entry_slippage_fallback_pct"] == 0.10
    assert a["hard_stop_slippage_pct"] == 0.30
    assert a["trailing_slippage_pct"] == 0.15
    assert a["timeout_slippage_pct"] == 0.20
    assert a["cost_sensitivity_pct"] == [0.10, 0.18, 0.25]
    assert a["slippage_multipliers"] == [0.5, 1.0, 1.5]
    assert a["cost_model_version"] == cm.COST_MODEL_VERSION


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("HARD_STOP", 0.30),
        ("TRAILING", 0.15),
        ("TIMEOUT", 0.20),
        ("SLIPPAGE_GUARD", 0.30),
    ],
)
def test_base_slippage_pct_maps_reason(reason, expected):
    assert cm.base_slippage_pct(reason) == expected


def test_base_slippage_pct_unknown_reason_falls_back_to_hard_stop():
    # Unknown reasons must not silently understate cost — use the most adverse.
    assert cm.base_slippage_pct("SOMETHING_ELSE") == 0.30


def test_gross_pct():
    assert cm.pnl_breakdown(10_000, 10_300, "TRAILING")["gross_pct"] == pytest.approx(3.0)


def test_cost_net_uses_default_base_cost():
    b = cm.pnl_breakdown(10_000, 10_300, "TRAILING")
    assert b["cost_net_pct"] == pytest.approx(3.0 - 0.18)


@pytest.mark.parametrize("cost", [0.10, 0.18, 0.25])
def test_cost_net_for_each_sensitivity_cost(cost):
    b = cm.pnl_breakdown(10_000, 10_300, "TRAILING", cost_pct=cost)
    assert b["cost_net_pct"] == pytest.approx(3.0 - cost)


@pytest.mark.parametrize("mult", [0.5, 1.0, 1.5])
def test_conservative_net_for_each_slippage_multiplier(mult):
    b = cm.pnl_breakdown(10_000, 10_300, "TRAILING", slippage_mult=mult)
    assert b["conservative_net_pct"] == pytest.approx(3.0 - 0.18 - 0.15 * mult)


def test_three_columns_present_together_hard_stop():
    b = cm.pnl_breakdown(10_000, 9_800, "HARD_STOP", cost_pct=0.25, slippage_mult=1.5)
    assert b["gross_pct"] == pytest.approx(-2.0)
    assert b["cost_net_pct"] == pytest.approx(-2.0 - 0.25)
    assert b["conservative_net_pct"] == pytest.approx(-2.0 - 0.25 - 0.30 * 1.5)


def test_entry_slippage_not_applied_by_default():
    b = cm.pnl_breakdown(10_000, 10_300, "TRAILING")
    assert b["entry_slippage_pct"] == pytest.approx(0.0)
    assert b["conservative_net_pct"] == pytest.approx(3.0 - 0.18 - 0.15)


@pytest.mark.parametrize("mult", [0.5, 1.0, 1.5])
def test_entry_slippage_applied_deducts_from_conservative_only(mult):
    base = cm.pnl_breakdown(10_000, 10_300, "TRAILING", slippage_mult=mult)
    applied = cm.pnl_breakdown(
        10_000, 10_300, "TRAILING", slippage_mult=mult, apply_entry_slippage=True
    )
    # gross와 cost-only net은 그대로다.
    assert applied["gross_pct"] == pytest.approx(base["gross_pct"])
    assert applied["cost_net_pct"] == pytest.approx(base["cost_net_pct"])
    # conservative net만 진입 슬리피지(민감도 배수 적용)만큼 추가 차감된다.
    assert applied["entry_slippage_pct"] == pytest.approx(0.10 * mult)
    assert applied["conservative_net_pct"] == pytest.approx(
        base["conservative_net_pct"] - 0.10 * mult
    )


def test_entry_slippage_custom_value():
    b = cm.pnl_breakdown(
        10_000, 10_300, "HARD_STOP",
        apply_entry_slippage=True,
        entry_slippage_pct=0.20,
        slippage_mult=1.0,
    )
    assert b["entry_slippage_pct"] == pytest.approx(0.20)
    assert b["conservative_net_pct"] == pytest.approx(3.0 - 0.18 - 0.30 - 0.20)


def test_entry_zero_is_rejected():
    with pytest.raises(ValueError):
        cm.pnl_breakdown(0, 10_000, "TRAILING")


def test_rounding_is_stable():
    b = cm.pnl_breakdown(10_000, 10_333, "TIMEOUT")
    assert b["gross_pct"] == pytest.approx(3.33, abs=1e-9)
