from dataclasses import dataclass, field

from src.utils.logger import log


@dataclass
class SpikeFilter:
    """
    시세 스파이크 필터 (PRD §6-5).
    단일 틱 ±3% 초과 변동은 무시.
    연속 2틱 이상 같은 방향이면 정상 데이터로 판정.
    """

    threshold_pct: float = 0.03
    _prev_price: float = field(default=0.0, init=False, repr=False)
    _spike_streak: int = field(default=0, init=False, repr=False)

    def is_valid(self, price: float, ticker: str | None = None) -> bool:
        if self._prev_price == 0.0:
            self._prev_price = price
            return True

        change = abs(price - self._prev_price) / self._prev_price

        if change > self.threshold_pct:
            self._spike_streak += 1
            if self._spike_streak >= 2:
                # 연속 2틱 → 실제 급등락으로 인정
                self._prev_price = price
                self._spike_streak = 0
                return True
            log(
                "PRICE_SPIKE_FILTERED", level="WARN", ticker=ticker,
                received_price=price, prev_price=self._prev_price,
                change_pct=round(change * 100, 3),
            )
            return False

        self._spike_streak = 0
        self._prev_price = price
        return True
