from pathlib import Path

from scripts.kis_phase6_safety_audit import audit

SAFE_CONFIG = """
[mcp_servers.kis-code-assistant]
command = "npx"
args = ["-y", "@koreainvestment/kis-code-assistant-mcp@0.1.1"]
enabled = true
required = false
enabled_tools = ["search_domestic_stock_api", "read_source_code"]
"""


def _project(tmp_path: Path, *, with_config: bool = True) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / ".codex").mkdir()
    (tmp_path / "main.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (tmp_path / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".codex/\n", encoding="utf-8")
    (tmp_path / "docs" / "DEV_ENV.md").write_text(
        "09:00~09:11 별도 PAPER 키 봇 중지\n"
        "codex mcp remove kis-code-assistant\n자격 증명 폐기\n",
        encoding="utf-8",
    )
    (tmp_path / "docs" / "KIS_INCIDENT_AUDIT.md").write_text(
        "조사 시각(KST) 계정 구분 봇 상태 사용 경로\n", encoding="utf-8"
    )
    (tmp_path / "docs" / "SPRINT_KIS_MCP.md").write_text("Phase 6\n", encoding="utf-8")
    if with_config:
        (tmp_path / ".codex" / "config.toml").write_text(SAFE_CONFIG, encoding="utf-8")
    return tmp_path


def test_audit_accepts_isolated_code_assistant_config(tmp_path: Path) -> None:
    result = audit(_project(tmp_path))

    assert result["status"] == "PASS"
    assert result["config"]["enabled_tool_count"] == 2
    assert result["runtime"]["reference_count"] == 0
    assert result["external_call_count"] == 0


def test_audit_accepts_config_absent_and_rejects_runtime_dependency(tmp_path: Path) -> None:
    root = _project(tmp_path, with_config=False)

    absent = audit(root, require_config_absent=True)
    assert absent["status"] == "PASS"
    assert absent["config"]["status"] == "ABSENT"

    (root / "src" / "app.py").write_text("import mcp_runtime\n", encoding="utf-8")
    dependent = audit(root, require_config_absent=True)
    assert dependent["status"] == "FAIL"
    assert dependent["runtime"]["references"] == ["src/app.py:1"]
