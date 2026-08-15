"""비용·슬리피지 가정과 손익 분해 — 순수 계산(보고 전용).

PAPER 전략 개선 계획 §2 초기 연구 상수를 코드로 고정한다. 이 상수는 실제 요율의
단정이 아니라 보수적인 비교 가정이며, 주문 판단에는 절대 사용하지 않는다. 불변 DB
설정은 baseline_experiment가 이 모듈의 ``cost_assumptions()``를 ``strategy_configs``
JSON에 임베드해 config 해시로 고정한다. 상수가 바뀌면 해시가 달라져 새 config_id로
전량 재생하며 기존 결과를 덮어쓰지 않는다.
"""

from __future__ import annotations

COST_MODEL_VERSION = "cost-model-1"

# 매수·매도 수수료·세금 총합 연구 가정 (%p)
BASE_ROUND_TRIP_COST_PCT = 0.18
# 실행 가능한 매도호가가 없을 때 매수 불리 조정 (%p)
ENTRY_SLIPPAGE_FALLBACK_PCT = 0.10
# 청산 사유별 가상 매도 불리 조정 (%p)
HARD_STOP_SLIPPAGE_PCT = 0.30
TRAILING_SLIPPAGE_PCT = 0.15
TIMEOUT_SLIPPAGE_PCT = 0.20

# 민감도 격자
COST_SENSITIVITY_PCT = (0.10, 0.18, 0.25)
SLIPPAGE_MULTIPLIERS = (0.5, 1.0, 1.5)

# 청산 사유 → 기본 슬리피지(%p). SLIPPAGE_GUARD는 하드스탑과 동일한 급락 청산이다.
_REASON_SLIPPAGE = {
    "HARD_STOP": HARD_STOP_SLIPPAGE_PCT,
    "SLIPPAGE_GUARD": HARD_STOP_SLIPPAGE_PCT,
    "TRAILING": TRAILING_SLIPPAGE_PCT,
    "TIMEOUT": TIMEOUT_SLIPPAGE_PCT,
}


def base_slippage_pct(close_reason: str) -> float:
    """청산 사유별 기본 슬리피지(%p). 미확인 사유는 가장 불리한 값으로 보수 처리한다."""
    return _REASON_SLIPPAGE.get(str(close_reason), HARD_STOP_SLIPPAGE_PCT)


def pnl_breakdown(
    entry_price: float,
    exit_price: float,
    close_reason: str,
    *,
    cost_pct: float = BASE_ROUND_TRIP_COST_PCT,
    slippage_mult: float = 1.0,
    apply_entry_slippage: bool = False,
    entry_slippage_pct: float = ENTRY_SLIPPAGE_FALLBACK_PCT,
) -> dict:
    """총손익·비용 차감·보수 슬리피지 차감 손익률(%)을 함께 반환한다.

    ``apply_entry_slippage``는 실행 가능한 매도호가가 없어 매수를 불리하게 조정해야
    할 때만 켠다. 켜지면 conservative net에서만 진입 슬리피지(민감도 배수 적용)를
    추가 차감하고, gross와 cost-only net은 그대로 둔다. 적용된 진입 슬리피지 값은
    ``entry_slippage_pct``로 반환한다(미적용 시 0.0).
    """
    if not entry_price or entry_price <= 0:
        raise ValueError("entry_price must be positive")
    # 0 이하 청산가는 -100%를 조용히 만들어내므로 계산하지 않고 거부한다.
    if not exit_price or exit_price <= 0:
        raise ValueError("exit_price must be positive")

    gross_pct = (exit_price / entry_price - 1) * 100
    cost_net_pct = gross_pct - cost_pct
    exit_slippage = base_slippage_pct(close_reason) * slippage_mult
    applied_entry_slippage = (
        entry_slippage_pct * slippage_mult if apply_entry_slippage else 0.0
    )
    conservative_net_pct = gross_pct - cost_pct - exit_slippage - applied_entry_slippage
    return {
        "gross_pct": round(gross_pct, 6),
        "cost_net_pct": round(cost_net_pct, 6),
        "conservative_net_pct": round(conservative_net_pct, 6),
        "cost_pct": cost_pct,
        "slippage_pct": round(exit_slippage, 6),
        "entry_slippage_pct": round(applied_entry_slippage, 6),
        "close_reason": str(close_reason),
    }


def cost_assumptions() -> dict:
    """baseline config JSON에 임베드할 불변 가정 스냅샷."""
    return {
        "cost_model_version": COST_MODEL_VERSION,
        "base_round_trip_cost_pct": BASE_ROUND_TRIP_COST_PCT,
        "entry_slippage_fallback_pct": ENTRY_SLIPPAGE_FALLBACK_PCT,
        "hard_stop_slippage_pct": HARD_STOP_SLIPPAGE_PCT,
        "trailing_slippage_pct": TRAILING_SLIPPAGE_PCT,
        "timeout_slippage_pct": TIMEOUT_SLIPPAGE_PCT,
        "cost_sensitivity_pct": list(COST_SENSITIVITY_PCT),
        "slippage_multipliers": list(SLIPPAGE_MULTIPLIERS),
    }
