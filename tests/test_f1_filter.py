"""F1 갭 필터 경계값 유닛 테스트."""
from unittest.mock import AsyncMock, patch

import pytest

from src import state as _state_mod
from src.modules.f1_filter import GAP_MAX, GAP_MIN, _fetch_all_premarket, run


# ── 헬퍼 ──────────────────────────────────────────────────────────────

def _candidate(gap_pct: float, amount: float = 1e9, ticker: str = "TEST") -> dict:
    ep = 10_000.0
    return {
        "ticker": ticker,
        "expected_price": ep,
        "prev_close": round(ep / (1 + gap_pct)),
        "gap_pct": gap_pct,
        "avg_amount_5d": amount,
        "expected_amount": amount,
        "expected_qty": 1000,
    }


async def _run(candidates: list[dict]) -> list[dict]:
    """_fetch_all_premarket, notifier.send, db.record_skip 모킹 후 run() 실행."""
    with (
        patch("src.modules.f1_filter._fetch_all_premarket", new_callable=AsyncMock, return_value=candidates),
        patch("src.notifier.send", new_callable=AsyncMock),
        patch("src.db.record_skip", new_callable=AsyncMock),
    ):
        return await run()


# ── 픽스처 ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clean_state():
    s = _state_mod.get()
    s.day_skip = False
    s.target_ticker = None
    yield
    s.day_skip = False
    s.target_ticker = None


# ── 갭 경계값 ─────────────────────────────────────────────────────────

async def test_gap_3_0_pct_passes():
    """정확히 3.0%(GAP_MIN) → 필터 통과."""
    result = await _run([_candidate(GAP_MIN)])
    assert len(result) == 1


async def test_gap_2_9_pct_excluded():
    """2.9%(GAP_MIN 미만) → 필터 제외, day_skip=True."""
    result = await _run([_candidate(0.029)])
    assert result == []
    assert _state_mod.get().day_skip is True


async def test_gap_7_0_pct_excluded():
    """정확히 7.0%(GAP_MAX) → 상단 경계 제외 (조건: < GAP_MAX)."""
    result = await _run([_candidate(GAP_MAX)])
    assert result == []


async def test_gap_6_99_pct_passes():
    """6.99%(GAP_MAX 직전) → 필터 통과."""
    result = await _run([_candidate(0.0699)])
    assert len(result) == 1


async def test_gap_7_1_pct_excluded():
    """7.1%(GAP_MAX 초과) → 필터 제외."""
    result = await _run([_candidate(0.071)])
    assert result == []


async def test_empty_raw_returns_empty_and_sets_day_skip():
    """원본 데이터 없음 → 빈 리스트, day_skip=True."""
    result = await _run([])
    assert result == []
    assert _state_mod.get().day_skip is True


# ── 유동성 필터 ───────────────────────────────────────────────────────

async def test_liquidity_top_10_pct_single_result():
    """10종목 통과 → 상위 10%(1개) 반환, 유동성 최고 종목 선택."""
    candidates = [
        _candidate(0.05, amount=float(i) * 1e9, ticker=f"TICK{i:02d}")
        for i in range(1, 11)  # i=1~10, 최댓값 i=10 → 10e9
    ]
    result = await _run(candidates)
    assert len(result) == 1
    assert result[0]["avg_amount_5d"] == pytest.approx(10e9)  # i=10 최고 유동성


async def test_liquidity_min_one_result():
    """1종목 통과 시 무조건 1개 반환 (max(1, floor(1*0.1)) = 1)."""
    result = await _run([_candidate(0.05)])
    assert len(result) == 1


async def test_liquidity_top_10_of_20():
    """20종목 통과 → 상위 10%(2개) 반환."""
    candidates = [
        _candidate(0.05, amount=float(i) * 1e9, ticker=f"TICK{i:02d}")
        for i in range(1, 21)
    ]
    result = await _run(candidates)
    assert len(result) == 2
    amounts = {c["avg_amount_5d"] for c in result}
    assert amounts == {20e9, 19e9}  # i=20, i=19


async def test_day_skip_returns_early():
    """day_skip=True 시 즉시 빈 리스트 반환 (API 호출 없음)."""
    _state_mod.get().day_skip = True
    mock_fetch = AsyncMock()
    with patch("src.modules.f1_filter._fetch_all_premarket", mock_fetch):
        result = await run()
    assert result == []
    mock_fetch.assert_not_awaited()


async def test_fetch_excludes_etf_etn_leverage_inverse_products():
    """KIS 원본 응답에서 ETF/ETN/레버리지/인버스 상품은 F1 후보에서 제외."""
    async def fake_get(*args, **kwargs):
        market = kwargs["params"]["fid_cond_mrkt_div_code"]
        if market == "Q":
            return {"rt_cd": "0", "output": []}
        return {
            "rt_cd": "0",
            "output": [
                {
                    "mksc_shrn_iscd": "252670",
                    "hts_kor_isnm": "KODEX 200선물인버스2X",
                    "prdy_ctrt": "4.35",
                    "stck_prpr": "72",
                    "avrg_vol": "1000000",
                    "acml_tr_pbmn": "72000000",
                    "acml_vol": "1000000",
                },
                {
                    "mksc_shrn_iscd": "0197X0",
                    "hts_kor_isnm": "SOL SK하이닉스선물단일종목인버스2X",
                    "prdy_ctrt": "4.20",
                    "stck_prpr": "7515",
                    "avrg_vol": "1000000",
                    "acml_tr_pbmn": "7515000000",
                    "acml_vol": "1000000",
                },
                {
                    "mksc_shrn_iscd": "005930",
                    "hts_kor_isnm": "삼성전자",
                    "prdy_ctrt": "4.00",
                    "stck_prpr": "80000",
                    "avrg_vol": "1000000",
                    "acml_tr_pbmn": "80000000000",
                    "acml_vol": "1000000",
                },
            ],
        }

    with patch("src.api.kis_rest.get", new=fake_get):
        result = await _fetch_all_premarket()

    assert [c["ticker"] for c in result] == ["005930"]
    assert result[0]["name"] == "삼성전자"
