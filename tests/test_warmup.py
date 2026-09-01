from src import warmup


def _bars(n, start=0):
    return [{"time": f"{9 + i // 60:02d}{i % 60:02d}00", "close": float(start + i)}
            for i in range(n)]


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
    assert warmup.meta(_bars(390), days=1) == {
        "warmup_days": 1, "warmup_bars": 390, "warmed": False,
    }


def test_meta_is_warmed_at_the_minimum():
    assert warmup.meta(_bars(warmup.WARMUP_MIN_BARS), days=1)["warmed"] is True


def test_meta_reports_zero_days_when_no_bars_were_prepended():
    """요청은 1일이었어도 실제로 붙은 봉이 없으면 0일로 정직하게 남긴다."""
    assert warmup.meta([], days=1) == {
        "warmup_days": 0, "warmup_bars": 0, "warmed": False,
    }
