from src import notifier


def test_stale_position_alert_is_operator_friendly():
    text = notifier._format_alert_text(
        "STALE_POSITION_DETECTED",
        level="CRIT",
        message="전일 상태 오류 의심. date=20260630",
    )

    assert "긴급: 전일 포지션 잔류 의심" in text
    assert "상황: 이전 거래일 상태 파일에 포지션 정보가 남아 있습니다." in text
    assert "조치: 계좌 보유 수량과 미체결 주문을 확인" in text
    assert "세부: 전일 상태 오류 의심. date=20260630" in text
    assert "코드: STALE_POSITION_DETECTED" in text


def test_unknown_alert_keeps_event_code_but_still_has_readable_header():
    text = notifier._format_alert_text("SOME_NEW_EVENT", level="WARN")

    assert "확인: Some New Event" in text
    assert "코드: SOME_NEW_EVENT" in text
