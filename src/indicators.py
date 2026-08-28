"""지표 엔진 — 순수 함수만 둔다.

상태도 I/O도 없으므로 테스트가 결정적이고, 오프라인 재생 검증과 실시간이
같은 코드를 탄다. 값이 설 수 없는 구간은 0이 아니라 None을 돌려준다 —
0으로 채우면 MACD 히스토그램의 부호 판정이 개장 직후 거짓 신호를 낸다.
"""


def _closes(bars: list[dict], field: str = "close") -> list[float]:
    return [float(b[field]) for b in bars]


def sma(bars: list[dict], period: int, field: str = "close") -> list[float | None]:
    """단순이동평균. 앞의 period-1개는 None.

    `field`는 거래량 이동평균 때문에 있다 — 증권사 차트가 거래량 패널에
    올리는 그 선이다. 기본값은 종가라 기존 호출부는 그대로다.
    """
    if period <= 0:
        raise ValueError(f"period must be positive: {period}")
    closes = _closes(bars, field)
    out: list[float | None] = [None] * len(closes)
    running = 0.0
    for i, c in enumerate(closes):
        running += c
        if i >= period:
            running -= closes[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(bars: list[dict], period: int) -> list[float | None]:
    """지수이동평균. 첫 값은 SMA(period)로 시드한다."""
    if period <= 0:
        raise ValueError(f"period must be positive: {period}")
    closes = _closes(bars)
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    alpha = 2.0 / (period + 1)
    prev = sum(closes[:period]) / period
    out[period - 1] = prev
    for i in range(period, len(closes)):
        prev = prev + alpha * (closes[i] - prev)
        out[i] = prev
    return out


def macd(
    bars: list[dict],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> list[dict]:
    """MACD선·시그널선·히스토그램.

    시그널선은 'MACD가 정의된 구간'만 모아 EMA를 걸고 원래 자리로 되돌린다.
    None을 0으로 채워 EMA에 넣으면 시그널선이 0 쪽으로 끌려간다.
    """
    fast_ema = ema(bars, fast)
    slow_ema = ema(bars, slow)
    macd_line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]

    defined_idx = [i for i, v in enumerate(macd_line) if v is not None]
    signal_line: list[float | None] = [None] * len(macd_line)
    if defined_idx:
        packed = [{"close": macd_line[i]} for i in defined_idx]
        for pos, value in enumerate(ema(packed, signal)):
            signal_line[defined_idx[pos]] = value

    rows = []
    for m, s in zip(macd_line, signal_line):
        hist = (m - s) if (m is not None and s is not None) else None
        rows.append({"macd": m, "signal": s, "hist": hist})
    return rows
