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


def _spread(keep, first=None, last=None):
    """한 세션을 ``keep``개로 고르게 솎는다. 양 끝은 항상 남긴다."""
    full = _session()
    if first is not None:
        full = [b for b in full if b["time"] >= first]
    if last is not None:
        full = [b for b in full if b["time"] <= last]
    step = (len(full) - 1) / (keep - 1)
    idx = {round(i * step) for i in range(keep)}
    assert len(idx) == keep, "인덱스가 겹치면 표본이 요청보다 적어진다"
    return [full[i] for i in sorted(idx)]


def _sparse_session(keep=265):
    """거래가 뜸한 종목의 완전한 하루 — 봉은 265개뿐이어도 개장부터 마감까지다."""
    return _spread(keep)


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


def test_a_merged_record_with_a_hole_is_not_warmed():
    """양 끝만 보면 뚫린다 — 백필은 기존 봉과 받은 봉을 먼저 합치고 완결성을 나중에 묻는다.

    레코더가 남긴 09:00~09:30 조각이 개장 근거를, 오후만 받은 잘린 조회가 마감 근거를
    대주면 한가운데가 빈 기록이 완결로 통과한다. 캐시 227개 중 68개가 그 조각 모양이다.
    """
    stub = _bars(31)                                  # 09:00~09:30
    afternoon = [b for b in _session() if b["time"] >= "110000"]
    merged = stub + afternoon

    assert len(merged) > warmup.WARMUP_MIN_BARS
    assert merged[0]["time"] == "090000"
    assert merged[-1]["time"] == "153000"
    assert warmup.usable(merged) == []
    assert warmup.meta(merged, days=1)["warmed"] is False


def test_a_recorder_file_that_stops_at_lunch_is_not_warmed():
    """09:00~13:00 레코더 파일은 봉이 240개라 수렴 하한은 넘지만 마감에 닿지 않는다.

    이 경우가 없으면 마감 검사를 지워도 테스트가 전부 통과한다 — 다른 조각 픽스처는
    전부 봉 수가 적어 하한에서 먼저 걸리기 때문이다.
    """
    lunch = _spread(240, last="130000")

    assert len(lunch) > warmup.WARMUP_MIN_BARS
    assert lunch[0]["time"] == "090000"
    assert lunch[-1]["time"] <= "130000"
    assert warmup.usable(lunch) == []


def test_a_whole_day_with_too_few_bars_is_not_warmed():
    """개장~마감을 덮어도 봉이 수렴 하한에 못 미치면 시드가 남는다.

    봉 수를 ``WARMUP_MIN_BARS``에서 끌어오면 안 된다 — 상수를 바꾸면 픽스처도 같이
    움직여 아무것도 고정하지 못한다. 150은 스펙 §4.1 표의 228보다 확실히 아래다.
    """
    thin = _spread(150)

    assert thin[0]["time"] == "090000"
    assert thin[-1]["time"] == "153000"
    assert warmup.usable(thin) == []


def test_the_convergence_floor_is_the_spec_value():
    """수렴 하한은 스펙 §4.1 표에서 온 값이다 — 바꾸려면 그 표를 다시 계산해야 한다.

    EMA26 평활계수 2/27에서 228봉이면 시드 잔존이 2.4e-8이다. 이 단언이 없으면
    하한을 임의로 낮춰도 아무 테스트도 울지 않는다.
    """
    assert warmup.WARMUP_MIN_BARS == 228
    assert (1 - 2 / 27) ** warmup.WARMUP_MIN_BARS < 3e-8


def test_the_closing_auction_gap_is_not_a_hole():
    """15:20~15:30은 단일가라 비어 있는 것이 정상이다 — 공백으로 세면 안 된다."""
    session = _session()

    assert session[-2]["time"] == "151900"
    assert session[-1]["time"] == "153000"
    assert warmup.usable(session) == session


def test_a_morning_dense_record_with_a_lone_close_bar_is_not_warmed():
    """단일가 구간을 공백 검사에서 빼기만 하면 꼬리가 뚫린다.

    오전만 촘촘하고 종가 한 봉이 붙은 기록은 양 끝을 다 닿는다. 가운데가 두 시간 넘게
    비어 있는데도 완결로 통과하면 안 된다 — 연속매매 끝(15:20)까지 이어졌는지도 본다.
    """
    morning = _bars(300)                              # 09:00~13:59
    record = morning + [{"time": "153000", "close": 1.0}]

    assert len(record) > warmup.WARMUP_MIN_BARS
    assert record[0]["time"] == "090000"
    assert record[-1]["time"] == "153000"
    assert warmup.usable(record) == []


def test_a_real_thirty_one_minute_gap_is_not_a_hole():
    """실제 세션에서 관측된 최대 공백은 31분이다(20260728 005930 10:13→10:44).

    공백 상한을 그 아래로 조이면 정상적인 날을 매번 다시 받는다 — 이 테스트가
    상한을 아래에서 고정한다.
    """
    session = _session()
    gapped = [b for b in session if not ("101400" <= b["time"] <= "104300")]

    assert len(gapped) == len(session) - 30
    assert warmup.usable(gapped) == gapped


def test_unsorted_bars_are_judged_the_same():
    """입력 순서에 판정이 흔들리면 안 된다."""
    session = _session()
    shuffled = session[200:] + session[:200]

    assert warmup.covers_session(shuffled) is True
