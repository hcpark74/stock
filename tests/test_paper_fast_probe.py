import json
from datetime import datetime as real_datetime
from unittest.mock import AsyncMock

import pytest

from src.modules import paper_fast_probe as probe


def _ranking_row(ticker: str, name: str, price: int) -> dict:
    return {
        "stck_shrn_iscd": ticker,
        "hts_kor_isnm": name,
        "stck_prpr": str(price),
        "prdy_ctrt": "4.0",
        "avrg_vol": "100000",
        "acml_tr_pbmn": str(price * 100000),
    }


def _multi_row(
    ticker: str,
    name: str,
    price: int,
    prev_close: int,
    *,
    expected_qty: int = 100000,
    ask: int | None = None,
) -> dict:
    return {
        "inter_shrn_iscd": ticker,
        "inter_kor_isnm": name,
        "inter2_prpr": str(price),
        "inter2_prdy_clpr": str(prev_close),
        "inter2_askp": str(ask if ask is not None else price),
        "intr_antc_cntg_prdy_ctrt": str(round((price / prev_close - 1) * 100, 2)),
        "intr_antc_vol": str(expected_qty),
        "prdy_ctrt": str(round((price / prev_close - 1) * 100, 2)),
        "acml_vol": str(expected_qty),
        "acml_tr_pbmn": str(price * expected_qty),
        "hour_cls_code": "1",
        "mrkt_trtm_cls_name": "",
    }


def test_probe_is_hard_gated_to_explicit_paper_mode(monkeypatch):
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_SHADOW", "1")
    monkeypatch.setenv("PAPER_FAST_HYBRID", "1")
    monkeypatch.setenv("DRY_RUN", "0")

    monkeypatch.setenv("KIS_MODE", "REAL")
    assert probe.enabled() is False
    assert probe.shadow_enabled() is False
    assert probe.hybrid_enabled() is False

    monkeypatch.setenv("KIS_MODE", "PAPER")
    assert probe.enabled() is True
    assert probe.shadow_enabled() is True
    assert probe.hybrid_enabled() is True

    monkeypatch.setenv("DRY_RUN", "1")
    assert probe.enabled() is False
    assert probe.shadow_enabled() is False
    assert probe.hybrid_enabled() is False


def test_multi_params_supports_at_most_thirty_tickers():
    tickers = [f"{index:06d}" for index in range(35)]
    params = probe._multi_params(tickers)

    assert len(params) == 60
    assert params["FID_INPUT_ISCD_1"] == "000000"
    assert params["FID_INPUT_ISCD_30"] == "000029"
    assert "FID_INPUT_ISCD_31" not in params


@pytest.mark.asyncio
async def test_prepare_records_four_public_calls_and_selects_shadow_shortlist(
    monkeypatch,
    tmp_path,
):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 8, 59, 45, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = []

    ranking_j = [
        _ranking_row("006340", "대원전선", 14400),
        _ranking_row("000001", "코스피후보", 10500),
    ]
    multi_j = [
        _multi_row("006340", "대원전선", 14400, 13730, expected_qty=300000),
        _multi_row("000001", "코스피후보", 10500, 10000, expected_qty=20000),
    ]
    ranking_q = [
        _ranking_row("477850", "마키나락스", 25200),
        _ranking_row("439960", "코스모로보틱스", 15000),
    ]
    multi_q = [
        _multi_row("477850", "마키나락스", 25200, 24250, expected_qty=50000),
        _multi_row("439960", "코스모로보틱스", 15000, 14310, expected_qty=40000),
    ]
    get = AsyncMock(
        side_effect=[
            {"rt_cd": "0", "msg_cd": "OK", "output": ranking_j},
            {"rt_cd": "0", "msg_cd": "OK", "output": multi_j},
            {"rt_cd": "0", "msg_cd": "OK", "output": ranking_q},
            {"rt_cd": "0", "msg_cd": "OK", "output": multi_q},
        ]
    )
    monkeypatch.setattr(probe.kis_rest, "get", get)

    selected = await probe.prepare()

    assert get.await_count == 4
    assert len(selected) == 4
    assert set(selected).issubset({"006340", "000001", "477850", "439960"})

    path = next(tmp_path.glob("*.jsonl"))
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == [
        "PAPER_FAST_PROBE_PREOPEN_START",
        "PAPER_FAST_PROBE_RANKING",
        "PAPER_FAST_PROBE_MULTI",
        "PAPER_FAST_PROBE_RANKING",
        "PAPER_FAST_PROBE_MULTI",
        "PAPER_FAST_PROBE_PREOPEN_DONE",
    ]
    assert records[-1]["markets"][0]["missing_tickers"] == []
    assert "authorization" not in path.read_text(encoding="utf-8").lower()
    assert "appsecret" not in path.read_text(encoding="utf-8").lower()


@pytest.mark.asyncio
async def test_prepare_skips_after_market_open(monkeypatch, tmp_path):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 0, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = ["006340"]
    get = AsyncMock()
    monkeypatch.setattr(probe.kis_rest, "get", get)

    assert await probe.prepare() == []

    get.assert_not_awaited()
    assert probe._prepared_tickers == []
    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "PAPER_FAST_PROBE_PREOPEN_SKIPPED"
    assert records[-1]["reason"] == "MARKET_OPEN"


@pytest.mark.asyncio
async def test_prepare_is_noop_in_real_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    get = AsyncMock()
    monkeypatch.setattr(probe.kis_rest, "get", get)

    assert await probe.prepare() == []
    get.assert_not_awaited()
    assert list(tmp_path.glob("*")) == []


@pytest.mark.asyncio
async def test_open_boundary_is_noop_in_real_mode(monkeypatch, tmp_path):
    monkeypatch.setenv("KIS_MODE", "REAL")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    get = AsyncMock()
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.observe_open_boundary()

    get.assert_not_awaited()
    assert list(tmp_path.glob("*")) == []


@pytest.mark.asyncio
async def test_open_boundary_observes_prepared_tickers_without_trading(
    monkeypatch,
    tmp_path,
):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 0, 300000, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_PROBE_OPEN_OFFSET_MS", "300")
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = ["006340", "477850", "439960"]
    get = AsyncMock(
        return_value={
            "rt_cd": "0",
            "msg_cd": "OK",
            "output": [
                _multi_row("006340", "대원전선", 14500, 13730, ask=14510),
                _multi_row("477850", "마키나락스", 25200, 24250, ask=25250),
                _multi_row("439960", "코스모로보틱스", 15480, 14310, ask=15490),
            ],
        }
    )
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.observe_open_boundary()

    get.assert_awaited_once()
    assert get.await_args.args[0] == probe.MULTI_PRICE_PATH
    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "PAPER_FAST_PROBE_OPEN_DONE"
    assert records[-1]["valid_ask_count"] == 3
    assert records[-1]["requested_tickers"] == ["006340", "477850", "439960"]
    assert records[-1]["shadow_candidate_count"] == 2


def test_multi_candidate_derives_expected_price_when_current_is_previous_close():
    row = _multi_row("005930", "삼성전자", 227500, 220000, expected_qty=1000)
    row["inter2_prpr"] = "220000"
    row["intr_antc_cntg_vrss"] = "7500"

    candidate = probe._candidate_from_multi(row, "J", {})

    assert candidate is not None
    assert candidate["current_price"] == 220000
    assert candidate["expected_price"] == 227500
    assert candidate["gap_pct"] == pytest.approx(0.0341)


@pytest.mark.parametrize(
    "name",
    ["KODEX 200", "TIGER 인버스", "시장대표 레버리지", "미국채 선물 ETF"],
)
def test_multi_candidate_excludes_non_common_stock_products(name):
    row = _multi_row("123456", name, 10300, 10000, expected_qty=1000)

    assert probe._candidate_from_multi(row, "J", {}) is None


def test_shadow_compare_records_top_three_overlap_without_mutating_candidates(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("PAPER_FAST_SHADOW", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    probe._open_candidates = [
        {"ticker": "005930", "gap_pct": 0.0341},
        {"ticker": "332570", "gap_pct": 0.031},
    ]
    legacy = [
        {"ticker": "005930", "gap_pct": 0.034},
        {"ticker": "319400", "gap_pct": 0.058},
    ]

    fields = probe.compare_with_legacy(legacy)

    assert fields["rank1_match"] is True
    assert fields["top3_overlap_count"] == 1
    assert legacy[0]["gap_pct"] == 0.034
    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "PAPER_FAST_SHADOW_COMPARE"


@pytest.mark.asyncio
async def test_open_boundary_skips_when_scheduler_is_too_late(monkeypatch, tmp_path):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 5, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_PROBE_OPEN_OFFSET_MS", "300")
    monkeypatch.setenv("PAPER_FAST_PROBE_OPEN_MAX_LATENESS_MS", "2500")
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = ["006340"]
    get = AsyncMock()
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.observe_open_boundary()

    get.assert_not_awaited()
    records = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["event"] == "PAPER_FAST_PROBE_OPEN_SKIPPED"
    assert records[-1]["reason"] == "TOO_LATE"


@pytest.mark.asyncio
async def test_open_boundary_recovers_tickers_from_preopen_record(
    monkeypatch,
    tmp_path,
):
    class FixedDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 7, 28, 9, 0, 0, 300000, tzinfo=probe.KST)

    monkeypatch.setenv("KIS_MODE", "PAPER")
    monkeypatch.setenv("PAPER_FAST_PROBE", "1")
    monkeypatch.setenv("DRY_RUN", "0")
    monkeypatch.setenv("PAPER_FAST_PROBE_DIR", str(tmp_path))
    monkeypatch.setenv("PAPER_FAST_PROBE_OPEN_OFFSET_MS", "300")
    monkeypatch.setattr(probe, "datetime", FixedDateTime)
    probe._prepared_tickers = []
    path = tmp_path / "20260728.jsonl"
    path.write_text(
        json.dumps({
            "event": "PAPER_FAST_PROBE_PREOPEN_DONE",
            "selected_tickers": ["006340", "477850"],
        })
        + "\n",
        encoding="utf-8",
    )
    get = AsyncMock(return_value={"rt_cd": "0", "output": []})
    monkeypatch.setattr(probe.kis_rest, "get", get)

    await probe.observe_open_boundary()

    assert get.await_args.kwargs["params"]["FID_INPUT_ISCD_1"] == "006340"
    assert get.await_args.kwargs["params"]["FID_INPUT_ISCD_2"] == "477850"
