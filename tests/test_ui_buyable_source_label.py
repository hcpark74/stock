"""docs/html/assets/app.js의 buyableSourceLabel() UI 라벨 매핑 테스트.

백엔드(parse_asset_snapshot_response)가 반환하는 buyable_cash_source 값마다
UI가 올바른 한글 라벨을 표시하는지 실제 JS 함수를 node로 실행해 검증한다.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[1] / "docs" / "html" / "assets" / "app.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node 미설치")


def _buyable_source_label_fn() -> str:
    src = APP_JS.read_text(encoding="utf-8")
    m = re.search(r"function buyableSourceLabel\([^)]*\)\s*\{.*?\n\}", src, re.S)
    assert m, "app.js에서 buyableSourceLabel 함수를 찾지 못함"
    return m.group(0)


def _label_for(source) -> str:
    script = (
        _buyable_source_label_fn()
        + f"\nprocess.stdout.write(JSON.stringify(buyableSourceLabel({json.dumps(source)})));"
    )
    # Windows 기본 콘솔 인코딩(cp949) 대신 UTF-8로 stdin/stdout을 주고받는다.
    result = subprocess.run(
        ["node"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "source,expected",
    [
        ("ord_psbl_cash", "주문가능 현금"),
        ("dnca_tot_amt", "예수금 기준"),
        ("prvs_rcdl_excc_amt", "D+2 정산금 기준"),
        (None, "출처 대기"),
        ("unknown_field", "출처 대기"),
    ],
)
def test_buyable_source_label(source, expected):
    assert _label_for(source) == expected
