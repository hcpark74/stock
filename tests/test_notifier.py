from src import notifier


def test_stale_position_alert_is_operator_friendly():
    text = notifier._format_alert_text(
        "STALE_POSITION_DETECTED",
        level="CRIT",
        message="전일 상태 오류 발견. date=20260630",
    )

    assert "[CRIT] 긴급: 전일 포지션 오류 발견" in text
    assert "상황: 이전 거래일의 상태 파일이나 포지션 정보가 남아 있습니다." in text
    assert "조치: 계좌 보유 수량과 미체결 주문을 확인" in text
    assert "메모: 전일 상태 오류 발견. date=20260630" in text
    assert "코드: STALE_POSITION_DETECTED" in text


def test_unknown_alert_keeps_event_code_but_still_has_readable_header():
    text = notifier._format_alert_text("SOME_NEW_EVENT", level="WARN")

    assert "[WARN] 확인: Some New Event" in text
    assert "코드: SOME_NEW_EVENT" in text


def test_f2_retry_alerts_are_operator_friendly():
    retry = notifier._format_alert_text("F2_FAIL_F1_RETRY", level="WARN")
    exhausted = notifier._format_alert_text("F2_RETRY_EXHAUSTED", level="WARN")

    assert "F2 실패 후 F1 재시도" in retry
    assert "TARGET_LOCKED 또는 F2_RETRY_EXHAUSTED" in retry
    assert "F1 재시도 후 대상 없음" in exhausted
    assert "오늘 자동 진입은 종료" in exhausted
