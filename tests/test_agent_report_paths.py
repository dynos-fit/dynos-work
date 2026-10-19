"""Contract: every agent prompt that names an audit report path uses the canonical literal path."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = ".dynos/task-{id}/audit-reports/{auditor_name}-{model}-attempt-{n}.json"
HEREDOC = f'cat > "{CANONICAL}" <<\'JSON\''
REPORT_JSON = re.compile(r"\.dynos/task-\{id\}/audit-reports/[^\s`\"']*\.json")


def _files() -> list[Path]:
    files = sorted((ROOT / "agents").glob("*.md"))
    files.append(ROOT / "agents" / "_shared" / "audit-report.md")
    return files


def test_agent_report_paths_match_shared_contract() -> None:
    files = _files()
    assert len(files) > 10
    auditors_checked = 0
    for path in files:
        text = path.read_text(encoding="utf-8")
        rel = path.relative_to(ROOT)
        for line in text.splitlines():
            assert not ("audit-reports" in line and "{timestamp}" in line), f"{rel}: {line}"
            assert "$TIMESTAMP" not in line and "$(date" not in line or "audit-reports" not in line, f"{rel}: {line}"
        for match in REPORT_JSON.findall(text):
            assert match == CANONICAL, f"{rel}: non-canonical report path {match}"
        if path.name.endswith("-auditor.md") and CANONICAL in text:
            auditors_checked += 1
            assert HEREDOC in text, f"{rel}: missing literal-path heredoc example"
            assert "`{auditor_name}`" in text and "`{model}`" in text and "`{n}`" in text, (
                f"{rel}: placeholders not defined"
            )
            assert "literal `default`" in text or "literal `default` when unknown" in text or "`default`" in text
    assert auditors_checked >= 20, auditors_checked
