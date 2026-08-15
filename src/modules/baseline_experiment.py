"""기동 시 canonical 기준선 실험·설정 등록.

DB init 직후 1회 호출한다. 현재 전략 지문(release.strategy_fingerprint)만으로
기준선을 열며, 과거·NULL 지문 거래와 섞지 않는다. baseline config에는 비밀·계좌·
경로가 들어가지 않고, F1/청산 임계값과 비용·슬리피지 가정만 임베드한다. 상수가
바뀌면 config 해시가 달라져 새 config_id·새 experiment_id로 기준선을 다시 연다.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from src import db, release
from src.api import kis_rest
from src.modules import cost_model, f1_selector, f4_tracking
from src.schedule_times import F5_EXEC_H, F5_EXEC_M

KST = ZoneInfo("Asia/Seoul")

_active_experiment_id: str | None = None

_PRIMARY_HYPOTHESIS = "트레일 폭 1.5% 대 2.0% (나머지 설정 고정)"
_PRIMARY_METRIC = "cost-net paired trail-width 1.5 vs 2.0"


def active_experiment_id() -> str | None:
    """프로세스 수명 동안 고정된 활성 기준선 experiment_id."""
    return _active_experiment_id


def _force_trailing_hhmm() -> str:
    return f"{f4_tracking.FORCE_TRAILING_HOUR:02d}:{f4_tracking.FORCE_TRAILING_MINUTE:02d}"


def canonical_config(
    *, step_trail: float | None = None, fingerprint: str | None = None
) -> dict:
    """코드 상수에서 읽은 현재 전략 설정 + 비용 가정 + 현재 지문. 비밀값 미포함.

    ``fingerprint``를 config에 포함해 해시가 현재 코드 지문에 바인딩되게 한다. 같은
    값이라도 지문이 다르면 새 config 해시가 되어, 옛 code_fingerprint를 가진 행을
    재사용하는 문제를 막는다. 고정 실효 KIS 호출 간격도 포함한다(PAPER 기본 1.1초).
    """
    return {
        "f1_gap_min": f1_selector.GAP_MIN,
        "f1_gap_core_max": f1_selector.GAP_CORE_MAX,
        "f1_gap_hard_max": f1_selector.GAP_HARD_MAX,
        "f1_min_expected_amount": f1_selector.MIN_EXPECTED_AMOUNT,
        "f1_high_gap_min_expected_amount": f1_selector.HIGH_GAP_MIN_EXPECTED_AMOUNT,
        "first_step": f4_tracking.STEP_SIZE,
        "step_trail": f4_tracking.STEP_TRAIL if step_trail is None else step_trail,
        "comparison_step_trail": f4_tracking.TRAILING_SHADOW_BASELINE_TRAIL,
        "hard_stop_ratio": f4_tracking.HARD_STOP_RATIO,
        "force_trailing": _force_trailing_hhmm(),
        "f5_exec": f"{F5_EXEC_H:02d}:{F5_EXEC_M:02d}",
        "kis_concurrency": 1,
        "kis_rate_interval_sec": kis_rest.rate_interval(),
        "cost_assumptions": cost_model.cost_assumptions(),
        "strategy_fingerprint": fingerprint,
    }


async def ensure_baseline_experiment() -> str:
    """현재 지문의 기준선 실험·설정을 idempotent하게 등록하고 experiment_id 반환."""
    global _active_experiment_id

    fingerprint = release.strategy_fingerprint()
    baseline_config_id = await db.upsert_strategy_config(
        canonical_config(fingerprint=fingerprint),
        kind="PRIMARY",
        code_fingerprint=fingerprint,
    )
    candidate_config_id = await db.upsert_strategy_config(
        canonical_config(
            step_trail=f4_tracking.TRAILING_SHADOW_BASELINE_TRAIL,
            fingerprint=fingerprint,
        ),
        kind="EXPLORATORY",
        code_fingerprint=fingerprint,
        parent_config_id=baseline_config_id,
    )

    experiment_id = f"baseline-{fingerprint}"
    today = datetime.now(KST).strftime("%Y%m%d")
    await db.register_experiment(
        experiment_id,
        hypothesis=_PRIMARY_HYPOTHESIS,
        baseline_config_id=baseline_config_id,
        candidate_config_id=candidate_config_id,
        primary_metric=_PRIMARY_METRIC,
        learn_start_date=today,
        stop_conditions={
            "consecutive_losses": 4,
            "single_trade_conservative_net_pct": -3.5,
            "recent_10d_conservative_net_pct": -8.0,
        },
        safety_limits={
            "candidate_required_field_rate_pct": 95,
            "path_complete_min_days_of_10": 8,
        },
        strategy_fingerprint=fingerprint,
        status="ACTIVE",
    )
    _active_experiment_id = experiment_id
    return experiment_id
