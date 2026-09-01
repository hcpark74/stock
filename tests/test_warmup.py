from src import warmup


def _bars(n, start=0):
    return [{"time": f"{9 + i // 60:02d}{i % 60:02d}00", "close": float(start + i)}
            for i in range(n)]


def _session(n=381):
    """실제 한 세션의 모양 — 연속매매 09:00~15:19 380봉 + 단일가 종가 15:30 1봉.

    09:00~15:30을 391분으로 세면 안 된다. 15:20~15:30은 단일가라 그 사이에
    봉이 없다.
    """
    out = [
        {"time": f"{9 + i // 60:02d}{i % 60:02d}00", "close": float(i)}
        for i in range(n - 1)
    ]
    out.append({"time": "153000", "close": float(n)})
    return out


def _sparse_session(keep=265):
    """거래가 뜸한 종목의 완전한 하루 — 봉은 265개뿐이어도 개장부터 마감까지다."""
    full = _session()
    step = len(full) / (keep - 2)
    picked = [full[int(i * step)] for i in range(keep - 2)]
    return [full[0]] + picked[1:] + [full[-1]]


def test_combine_prepends_and_reports_offset():
    warm, day = _bars(3), _bars(2, start=100)

    merged, offset = warmup.combine(warm, day)

    assert offset == 3
    assert merged[offset:] == day
    assert merged[:offset] == warm


def test_combine_with_no_warmup_is_the_day_itself():
    day = _bars(2)

    merged, offset = warmup.combine([], day)

    assert offset == 0
    assert merged == day


def test_combine_does_not_alias_the_inputs():
    """호출부가 반환값을 고쳐도 원본 리스트가 바뀌면 안 된다."""
    warm, day = _bars(1), _bars(1)

    merged, _ = warmup.combine(warm, day)
    merged.append({"time": "150000"})

    assert len(warm) == 1
    assert len(day) == 1


def test_meta_is_not_warmed_below_the_minimum():
    assert warmup.meta(_bars(warmup.WARMUP_MIN_BARS - 1), days=1) == {
        "warmup_days": 1, "warmup_bars": warmup.WARMUP_MIN_BARS - 1, "warmed": False,
    }


def test_meta_is_warmed_at_the_minimum():
    assert warmup.meta(_session(), days=1)["warmed"] is True


def test_meta_reports_zero_days_when_no_bars_were_prepended():
    """요청은 1일이었어도 실제로 붙은 봉이 없으면 0일로 정직하게 남긴다."""
    assert warmup.meta([], days=1) == {
        "warmup_days": 0, "warmup_bars": 0, "warmed": False,
    }


def test_usable_is_empty_just_below_the_threshold():
    warm = _bars(warmup.WARMUP_MIN_BARS - 1)
    assert warmup.usable(warm) == []


def test_usable_passes_through_at_the_threshold():
    warm = _session()
    assert warmup.usable(warm) == warm


def test_usable_of_empty_is_empty():
    assert warmup.usable([]) == []


def test_a_real_full_session_is_warmed():
    """실제 한 세션은 381봉이다. 391봉을 요구하면 워밍업은 영영 적용되지 않는다."""
    session = _session()

    assert len(session) == 381
    assert warmup.usable(session) == session
    assert warmup.meta(session, days=1)["warmed"] is True


def test_a_sparse_but_whole_day_is_warmed():
    """거래가 뜸해 265봉뿐이어도 개장부터 마감까지면 데운 것이다.

    봉 수만 보면 이런 날이 아침 캡처 찌꺼기와 구분되지 않는다.
    """
    sparse = _sparse_session()

    assert len(sparse) < 300
    assert warmup.usable(sparse) == sparse
    assert warmup.meta(sparse, days=1)["warmed"] is True


def test_a_morning_only_capture_is_not_warmed():
    """09:00~09:30 캡처 찌꺼기는 마감에 닿지 않으므로 데운 것이 아니다."""
    morning = _bars(31)

    assert warmup.usable(morning) == []
    assert warmup.meta(morning, days=1)["warmed"] is False


def test_a_fetch_that_never_reached_the_open_is_not_warmed():
    """봉 수가 넉넉해도 개장 무렵에 닿지 않으면 잘린 조회다."""
    truncated = _session()[120:]

    assert len(truncated) > warmup.WARMUP_MIN_BARS
    assert truncated[-1]["time"] == "153000"
    assert warmup.usable(truncated) == []


def test_bars_without_times_are_not_warmed():
    """시각이 없는 봉으로는 세션을 덮었는지 판정할 수 없다 — 데우지 않는다."""
    assert warmup.usable([{"close": 1.0}] * 400) == []


def test_a_day_without_a_closing_auction_bar_is_warmed():
    """종가 단일가에 체결이 없어 15:19에서 끝나는 날이 있다 — 잘린 것이 아니다."""
    no_close = _session()[:-1]

    assert no_close[-1]["time"] == "151900"
    assert warmup.usable(no_close) == no_close
