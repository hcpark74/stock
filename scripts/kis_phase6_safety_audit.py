"""Phase 6 KIS MCP 운영 안전성 정적 감사.

KIS API나 MCP를 호출하지 않는다. 프로젝트 MCP 설정, 런타임 의존성, Git 제외와
운영 문서의 필수 안전 규칙만 읽어 집계한다.
"""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_TOOL_PARTS = {"order", "cancel", "modify", "buy", "sell", "balance", "trading"}
SECRET_MARKERS = {
    "KIS_APP_KEY",
    "KIS_APP_SECRET",
    "KIS_ACCOUNT_NO",
    "KIS_ACCT_NO",
    "ACCESS_TOKEN",
}
RUNTIME_REF_RE = re.compile(r"mcp|kis-code-assistant|\.codex", re.IGNORECASE)


def _audit_config(root: Path, require_absent: bool) -> dict:
    path = root / ".codex" / "config.toml"
    if not path.exists():
        return {
            "status": "ABSENT",
            "safe": True,
            "required_absent": require_absent,
            "registered_server_count": 0,
        }

    text = path.read_text(encoding="utf-8")
    try:
        config = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {"status": "INVALID_TOML", "safe": False, "required_absent": require_absent}
    servers = config.get("mcp_servers") or {}
    server = servers.get("kis-code-assistant") or {}
    tools = server.get("enabled_tools") or []
    forbidden_tools = sorted(
        tool
        for tool in tools
        if any(part in str(tool).lower() for part in FORBIDDEN_TOOL_PARTS)
    )
    secret_markers = sorted(marker for marker in SECRET_MARKERS if marker in text.upper())
    args = server.get("args") or []
    package_pinned = any(
        str(arg) == "@koreainvestment/kis-code-assistant-mcp@0.1.1" for arg in args
    )
    safe = (
        not require_absent
        and set(servers) == {"kis-code-assistant"}
        and server.get("command") == "npx"
        and package_pinned
        and server.get("required") is False
        and bool(tools)
        and not forbidden_tools
        and not secret_markers
    )
    return {
        "status": "PRESENT",
        "safe": safe,
        "required_absent": require_absent,
        "registered_server_count": len(servers),
        "server_names": sorted(servers),
        "package_pinned": package_pinned,
        "required": server.get("required"),
        "enabled_tool_count": len(tools),
        "forbidden_tools": forbidden_tools,
        "secret_marker_count": len(secret_markers),
    }


def _audit_runtime(root: Path) -> dict:
    files = [root / "main.py", *sorted((root / "src").rglob("*.py"))]
    hits: list[str] = []
    for path in files:
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if RUNTIME_REF_RE.search(line):
                hits.append(f"{path.relative_to(root).as_posix()}:{line_no}")
    return {"safe": not hits, "reference_count": len(hits), "references": hits}


def _audit_gitignore(root: Path) -> dict:
    path = root / ".gitignore"
    entries = {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    ignored = ".codex/" in entries or ".codex" in entries
    return {"safe": ignored, "codex_directory_ignored": ignored}


def _audit_docs(root: Path) -> dict:
    dev = (root / "docs" / "DEV_ENV.md").read_text(encoding="utf-8")
    incident = (root / "docs" / "KIS_INCIDENT_AUDIT.md").read_text(encoding="utf-8")
    sprint = (root / "docs" / "SPRINT_KIS_MCP.md").read_text(encoding="utf-8")
    checks = {
        "forbidden_window": "09:00~09:11" in dev,
        "intraday_isolation": "별도 PAPER 키" in dev
        and ("봇 중지" in dev or "봇을 먼저 중지" in dev),
        "audit_record": all(
            token in incident
            for token in ("조사 시각(KST)", "계정 구분", "봇 상태", "사용 경로")
        ),
        "unregister_command": "codex mcp remove kis-code-assistant" in dev,
        "credential_disposal": "자격 증명 폐기" in dev,
        "phase6_section": "Phase 6" in sprint,
    }
    return {"safe": all(checks.values()), **checks}


def audit(root: Path, require_config_absent: bool = False) -> dict:
    config = _audit_config(root, require_config_absent)
    runtime = _audit_runtime(root)
    gitignore = _audit_gitignore(root)
    docs = _audit_docs(root)
    passed = config["safe"] and runtime["safe"] and gitignore["safe"] and docs["safe"]
    return {
        "status": "PASS" if passed else "FAIL",
        "read_only": True,
        "external_call_count": 0,
        "config": config,
        "runtime": runtime,
        "gitignore": gitignore,
        "docs": docs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 6 KIS MCP 운영 안전성 정적 감사")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--require-config-absent",
        action="store_true",
        help="MCP 설정 임시 제거 상태 검증",
    )
    args = parser.parse_args()
    result = audit(args.root.resolve(), require_config_absent=args.require_config_absent)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
