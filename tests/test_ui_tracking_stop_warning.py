"""docs/html/assets/app.js의 trackingStopConfirmText() 경고 문구 테스트.

'추적 종료' 버튼은 매도 후 가격 관측을 그날 내내 되돌릴 수 없게 중단시킨다.
수집이 끊긴 날은 전략 검증 표본에서 통째로 빠지므로(20260821·20260824가
실제로 그렇게 손실됐다), 확인 문구가 '유지됩니다'로 안심시키는 대신 남은
관측 시간과 되돌릴 수 없다는 사실을 먼저 알려야 한다.

실제 JS 함수를 node로 실행해 검증한다.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "docs" / "html" / "assets" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 미설치")

# 09:10 KST — 20260824에 실제로 버튼이 눌린 시각
MIN_0910 = 9 * 60 + 10
# 15:15 KST — tick_capture.CAPTURE_UNTIL, app.js의 _sessionEndMin 기본값
MIN_1515 = 15 * 60 + 15


def _confirm_text_fn() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"function trackingStopConfirmText\([^)]*\)\s*\{.*?\n\}", src, re.S)
    assert m, "app.js에서 trackingStopConfirmText 함수를 찾지 못함"
    return m.group(0)


def _text_for(now_min: int, session_end_min: int = MIN_1515) -> str:
    script = (
        _confirm_text_fn()
        + "\nprocess.stdout.write(JSON.stringify(trackingStopConfirmText("
        + f"{json.dumps(now_min)}, {json.dumps(session_end_min)})));"
    )
    result = subprocess.run(
        ["node"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


def test_states_remaining_observation_time() -> None:
    """09:10에 누르면 15:15까지 6시간 5분이 남았음을 알려야 한다."""
    text = _text_for(MIN_0910)
    assert "6시간 5분" in text
    assert "15:15" in text


def test_warns_that_stopping_is_irreversible_today() -> None:
    """재개 함수가 없으므로 되돌릴 수 없다는 사실을 명시해야 한다."""
    text = _text_for(MIN_0910)
    assert "다시 시작할 수 없습니다" in text


def test_does_not_lead_with_reassurance() -> None:
    """'유지됩니다'가 경고보다 앞서면 안 된다 — 그 문구가 손실을 유발했다."""
    text = _text_for(MIN_0910)
    keep_idx = text.find("유지됩니다")
    warn_idx = text.find("수집되지 않습니다")
    assert warn_idx >= 0, "손실 경고 문구가 없음"
    assert keep_idx >= 0, "유지 안내 문구가 없음"
    assert warn_idx < keep_idx, "안심 문구가 경고보다 앞서면 안 됨"


def test_mentions_strategy_validation_use() -> None:
    """왜 아까운 데이터인지 알려야 사용자가 판단할 수 있다."""
    assert "전략 검증" in _text_for(MIN_0910)


def test_remaining_time_under_one_hour_omits_hours() -> None:
    """14:50 → 25분. '0시간 25분'이 아니라 '25분'으로 읽혀야 한다."""
    text = _text_for(14 * 60 + 50)
    assert "25분" in text
    assert "0시간" not in text


def test_exactly_one_hour_left() -> None:
    """14:15 → 1시간. 분이 0이면 분을 붙이지 않는다."""
    text = _text_for(14 * 60 + 15)
    assert "1시간" in text
    assert "1시간 0분" not in text


def test_after_session_end_reports_no_remaining_time() -> None:
    """관측 창이 이미 닫혔으면 남은 시간을 주장하지 않는다."""
    text = _text_for(15 * 60 + 40)
    assert "수집되지 않습니다" not in text
    assert "종료" in text


def test_uses_session_end_argument_not_hardcoded() -> None:
    """마감이 앞당겨진 날(_sessionEndMin 갱신)에도 맞게 계산해야 한다."""
    text = _text_for(9 * 60 + 10, session_end_min=11 * 60 + 10)
    assert "2시간" in text
    assert "11:10" in text


# ── 버튼 툴팁 ────────────────────────────────────────────────────────


def _tooltip_fn() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"function trackingStopTooltip\([^)]*\)\s*\{.*?\n\}", src, re.S)
    assert m, "app.js에서 trackingStopTooltip 함수를 찾지 못함"
    return m.group(0)


def _tooltip_for(tracking_active: bool) -> str:
    script = (
        _tooltip_fn()
        + "\nprocess.stdout.write(JSON.stringify(trackingStopTooltip("
        + f"{json.dumps(tracking_active)})));"
    )
    result = subprocess.run(
        ["node"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


def test_active_tooltip_warns_instead_of_reassuring() -> None:
    """관측 중 툴팁이 '기존 차트 데이터는 유지됩니다'로 안심시키면 안 된다."""
    tip = _tooltip_for(True)
    assert "수집이 중단" in tip
    assert "되돌릴 수 없" in tip
    assert "기존 차트 데이터는 유지됩니다" not in tip


def test_inactive_tooltip_stays_informational() -> None:
    """이미 끝난 상태에서는 경고할 것이 없다."""
    tip = _tooltip_for(False)
    assert "종료되었습니다" in tip
    assert "되돌릴 수 없" not in tip
