"""H0STCNT0 다건 프레임 파싱 — 한 프레임에 체결이 여러 건 올 수 있다.

공식 구현(open-trading-api)은 프레임 데이터부를
``pd.read_csv(StringIO(data), sep="^", names=<46개 컬럼>)`` 로 읽고
``for _, row in df.iterrows()`` 로 **행을 순회**한다. 즉 한 프레임에 여러 체결
레코드가 담긴다. 기존 파서는 ``parts[3].split("^")`` 후 첫 레코드의 인덱스만
읽어 나머지를 조용히 버렸다.

체결이 폭주하는 구간에서 프레임이 묶일 가능성이 높고, 그 구간이 바로
트레일링·하드스탑 판정이 가장 민감한 구간이다.
"""

from src.api import kis_ws

# 공식 샘플 examples_llm/domestic_stock/ccnl_krx/ccnl_krx.py의 columns 기준
FIELD_COUNT = 46


def _record(ticker: str, hms: str, price: str, vol: str) -> str:
    """46필드짜리 체결 레코드 한 건."""
    f = [""] * FIELD_COUNT
    f[0] = ticker
    f[1] = hms
    f[2] = price
    f[12] = vol
    return "^".join(f)


def _frame(body: str, count: int) -> str:
    return f"0|H0STCNT0|{count:03d}|{body}"


def test_single_record_frame_yields_one_tick() -> None:
    raw = _frame(_record("005930", "091015", "10300", "7"), 1)
    ticks = kis_ws._parse_ticks(raw)
    assert len(ticks) == 1
    assert ticks[0]["ticker"] == "005930"
    assert ticks[0]["price"] == 10300.0
    assert ticks[0]["qty"] == 7


def test_newline_separated_records_all_parsed() -> None:
    """공식 구현이 read_csv 행으로 읽는 형태 — 줄바꿈 구분."""
    body = "\n".join([
        _record("005930", "091015", "10300", "7"),
        _record("005930", "091016", "10310", "3"),
        _record("005930", "091017", "10290", "11"),
    ])
    ticks = kis_ws._parse_ticks(_frame(body, 3))
    assert [t["price"] for t in ticks] == [10300.0, 10310.0, 10290.0]
    assert [t["qty"] for t in ticks] == [7, 3, 11]
    assert [t["exchange_time"] for t in ticks] == ["091015", "091016", "091017"]


def test_crlf_separated_records_all_parsed() -> None:
    """줄바꿈이 CRLF여도 빈 레코드가 끼지 않아야 한다."""
    body = "\r\n".join([
        _record("005930", "091015", "10300", "7"),
        _record("005930", "091016", "10310", "3"),
    ])
    ticks = kis_ws._parse_ticks(_frame(body, 2))
    assert [t["price"] for t in ticks] == [10300.0, 10310.0]


def test_trailing_newline_does_not_create_empty_tick() -> None:
    body = _record("005930", "091015", "10300", "7") + "\n"
    assert len(kis_ws._parse_ticks(_frame(body, 1))) == 1


def test_caret_only_concatenation_also_split_by_field_count() -> None:
    """구분자 형태에 의존하지 않는다 — ^로만 이어져도 46필드 단위로 나눈다."""
    body = "^".join([
        _record("005930", "091015", "10300", "7"),
        _record("005930", "091016", "10310", "3"),
    ])
    ticks = kis_ws._parse_ticks(_frame(body, 2))
    assert [t["price"] for t in ticks] == [10300.0, 10310.0]


def test_unexpected_field_count_falls_back_to_one_record() -> None:
    """필드 수가 46의 배수가 아니면 쪼개지 않는다(오정렬보다 기존 동작이 안전)."""
    short = ["005930", "091015", "10300"] + [""] * 9 + ["7"]  # 13필드
    ticks = kis_ws._parse_ticks(_frame("^".join(short), 1))
    assert len(ticks) == 1
    assert ticks[0]["price"] == 10300.0
    assert ticks[0]["qty"] == 7


def test_system_message_yields_no_ticks() -> None:
    assert kis_ws._parse_ticks('{"header":{"tr_id":"PINGPONG"}}') == []


def test_malformed_frame_yields_no_ticks() -> None:
    assert kis_ws._parse_ticks("0|H0STCNT0") == []


def test_each_record_keeps_its_own_raw_fields() -> None:
    """원시 보존이 레코드별로 분리돼야 한다 — 합쳐지면 소급 해석이 깨진다."""
    body = "\n".join([
        _record("005930", "091015", "10300", "7"),
        _record("005930", "091016", "10310", "3"),
    ])
    ticks = kis_ws._parse_ticks(_frame(body, 2))
    assert len(ticks[0]["raw"]) == FIELD_COUNT
    assert len(ticks[1]["raw"]) == FIELD_COUNT
    assert ticks[0]["raw"][1] == "091015"
    assert ticks[1]["raw"][1] == "091016"


async def test_subscribe_delivers_every_record_in_a_frame(monkeypatch) -> None:
    """구독 루프가 프레임당 한 건만 넘기면 안 된다."""
    body = "\n".join([
        _record("005930", "091015", "10300", "7"),
        _record("005930", "091016", "10310", "3"),
    ])
    delivered = []

    async def on_tick(tick):
        delivered.append(tick["price"])

    for tick in kis_ws._parse_ticks(_frame(body, 2)):
        await on_tick(tick)

    assert delivered == [10300.0, 10310.0]
    # subscribe가 _parse_tick(단건)이 아니라 _parse_ticks를 쓰는지 확인한다.
    import inspect
    src = inspect.getsource(kis_ws.subscribe)
    assert "_parse_ticks" in src
