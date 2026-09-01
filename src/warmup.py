"""지표 워밍업 — 전 거래일 봉을 지표 입력 앞에 붙이는 순수 계약.

봉을 읽어오는 일은 여기 없다. 백테스트는 캐시 파일에서, 실시간은 분봉
API에서 각자 가져와 이 함수에 넘긴다. 여기 I/O를 두면 테스트가 파일과
네트워크에 묶인다.

설계: docs/superpowers/specs/2026-09-01-indicator-warmup-design.md
"""

from __future__ import annotations

# EMA26 평활계수 2/27에서 228봉이면 시드의 잔존 영향이 2.4e-8이다 (스펙 §4.1 표).
# 가격이 수천 원대여도 표시 소수점 둘째 자리 아래로 내려가므로 수렴 요건은 여기서
# 이미 충족된다. 스펙이 적었던 391은 "09:00~15:30 = 391분"이라는 틀린 산수였다 —
# 15:20~15:30은 단일가라 그 사이에 봉이 없고, 완전한 하루는 연속매매 380봉 + 종가
# 1봉 = 381봉이다. 391을 요구하면 어떤 날도 통과하지 못해 워밍업이 영영 꺼진다.
WARMUP_MIN_BARS = 228

# 개장·마감에 닿았는지 보는 여유 구간.
#
# 개장 쪽 10분: 첫 체결이 09:00 정각이 아닌 종목이 있다. 이 검사가 잘린 조회를
# 잡는 쪽이다 — 조회는 마감 커서에서 역방향으로 미므로, 중간에 끊기면 없어지는
# 것은 언제나 아침이다.
#
# 마감 쪽 15:00: 종가 단일가에 체결이 없어 15:19에서 끝나는 날이 실제로 있다
# (20260730 368600, 368봉). 그런 날을 잘렸다고 보면 매번 다시 받는다. 이 검사가
# 걸러야 할 것은 레코더가 남긴 아침 조각(09:00~09:30)이고, 그것과는 5시간 반
# 떨어져 있다.
_OPEN_BY = "091000"
_CLOSE_FROM = "150000"


def combine(warm: list[dict], day: list[dict]) -> tuple[list[dict], int]:
    """워밍업 봉을 앞에 붙이고 당일 첫 봉의 인덱스를 함께 돌려준다.

    새 리스트를 만든다 — 호출부가 결과를 고쳐도 원본이 바뀌면 안 된다.
    """
    merged = list(warm) + list(day)
    return merged, len(warm)


def covers_session(warm: list[dict]) -> bool:
    """전 거래일 한 세션을 통째로 받았는가.

    봉 수만으로는 판정할 수 없다. 거래가 뜸한 종목은 완전한 하루도 265봉이라
    아침 캡처 찌꺼기와 개수로 구분되지 않고, 반대로 장중에 잘린 조회는 봉이
    넉넉해도 앞쪽이 통째로 비어 있다. 개장 무렵부터 마감까지 걸쳐 있는지를 본다.

    수렴 요건(``WARMUP_MIN_BARS``)도 함께 건다 — 세션을 덮었더라도 봉이 극단적으로
    적으면 시드가 남는다.
    """
    if len(warm) < WARMUP_MIN_BARS:
        return False
    times = [str(bar.get("time") or "") for bar in warm]
    times = [t for t in times if t]
    if len(times) < WARMUP_MIN_BARS:
        return False
    return min(times) <= _OPEN_BY and max(times) >= _CLOSE_FROM


def usable(warm: list[dict]) -> list[dict]:
    """세션을 덮지 못했으면 데우지 않는다.

    부분 워밍업은 옛 모드도 새 모드도 아닌 제3의 값을 만든다 — 28봉으로 시드한
    MACD는 증권사와도, 일 단위 초기화와도 다르다(스펙 §4.3). 발견된 봉 수는
    ``meta``가 계속 보고하므로 진단은 가능하다.
    """
    return warm if covers_session(warm) else []


def meta(warm: list[dict], days: int) -> dict:
    """워밍업 상태. 실제로 붙은 봉이 없으면 요청 일수와 무관하게 0일이다."""
    count = len(warm)
    return {
        "warmup_days": days if count else 0,
        "warmup_bars": count,
        "warmed": covers_session(warm),
    }
