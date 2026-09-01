"""지표 워밍업 — 전 거래일 봉을 지표 입력 앞에 붙이는 순수 계약.

봉을 읽어오는 일은 여기 없다. 백테스트는 캐시 파일에서, 실시간은 분봉
API에서 각자 가져와 이 함수에 넘긴다. 여기 I/O를 두면 테스트가 파일과
네트워크에 묶인다.

설계: docs/superpowers/specs/2026-09-01-indicator-warmup-design.md
"""

from __future__ import annotations

# 전 거래일 한 세션(09:00~15:30). EMA26 평활계수 2/27로 391봉이면 시드의
# 잔존 영향이 8.5e-14라 표시 정밀도 어디에서도 증권사와 갈라지지 않는다.
# 이보다 적게 붙으면 데운 셈 치지 않는다 (스펙 §4).
WARMUP_MIN_BARS = 391


def combine(warm: list[dict], day: list[dict]) -> tuple[list[dict], int]:
    """워밍업 봉을 앞에 붙이고 당일 첫 봉의 인덱스를 함께 돌려준다.

    새 리스트를 만든다 — 호출부가 결과를 고쳐도 원본이 바뀌면 안 된다.
    """
    merged = list(warm) + list(day)
    return merged, len(warm)


def meta(warm: list[dict], days: int) -> dict:
    """워밍업 상태. 실제로 붙은 봉이 없으면 요청 일수와 무관하게 0일이다."""
    count = len(warm)
    return {
        "warmup_days": days if count else 0,
        "warmup_bars": count,
        "warmed": count >= WARMUP_MIN_BARS,
    }
