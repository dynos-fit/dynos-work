"""Regressions from the 7.5.32 field report.

The headline (evidence findings escalating to FAILED) is covered in
test_repair_findings_ensemble.py, rule waivers in test_rule_waivers.py and
the planner per-spawn cap in test_pre_execution_latency_round2.py.

1. The repair-log builder queued one task per finding even when they all
   edited one shared file (nine serial batches for one root cause), and a
   finding with no file fell back to every file in the graph.
3. Plan gap analysis spent its scan budget inside ``backend/.venv`` and
   could not read annotated Data Model cells.
6. ``--tokens-used None`` (what the execute skill passes for an unknown
   count) was rejected by argparse.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))
sys.path.insert(0, str(ROOT / "tests"))

import ctl  # noqa: E402
from plan_gap_analysis import _iter_code_files, analyze_data_model  # noqa: E402
from test_field_report_7_5_31 import _project  # noqa: E402
from test_repair_findings_ensemble import DEEP, _report, _run_ctl, _setup, _single_entry  # noqa: E402


def _build(td: Path, tmp_path: Path) -> list[dict]:
    env = {"DYNOS_HOME": str(tmp_path / "dynos-home")}
    assert _run_ctl("run-audit-repair-cycle-plan", str(td), env=env).returncode == 0
    build = _run_ctl("run-repair-log-build", str(td), env=env)
    assert build.returncode == 0, build.stdout + build.stderr
    return json.loads((td / "repair-log.json").read_text())["batches"]


# ---------------------------------------------------------------------------
# 1. Repair-log grouping
# ---------------------------------------------------------------------------

def test_findings_on_one_file_become_one_task_led_by_the_most_severe(tmp_path: Path) -> None:
    td = _setup(tmp_path, [_single_entry("code-quality-auditor"), _single_entry("security-auditor")],
                stage="REPAIR_PLANNING")
    _project(td)
    _report(td, "code-quality-auditor", DEEP, [
        {"id": "CQ-1", "severity": "minor", "blocking": True, "location": "src/a.py:3",
         "description": "helper retries forever"},
        {"id": "CQ-2", "severity": "minor", "blocking": True, "location": "src/a.py:40",
         "description": "helper swallows errors"},
        {"id": "CQ-3", "severity": "minor", "blocking": True, "location": "web/app.tsx:1"},
    ])
    _report(td, "security-auditor", DEEP, [
        {"id": "SEC-1", "severity": "high", "blocking": True, "location": "src/a.py:7",
         "description": "helper logs the token"},
    ])

    batches = _build(td, tmp_path)

    assert len(batches) == 1 and batches[0]["parallel"] is True
    tasks = {t["finding_id"]: t for t in batches[0]["tasks"]}
    assert set(tasks) == {"SEC-1", "CQ-3"}
    lead = tasks["SEC-1"]
    assert lead["auditor"] == "security-auditor" and lead["severity"] == "high"
    assert lead["files_to_modify"] == ["src/a.py"]
    assert sorted((m["auditor"], m["id"]) for m in lead["merged_findings"]) == [
        ("code-quality-auditor", "CQ-1"), ("code-quality-auditor", "CQ-2"),
    ]
    assert "swallows errors" in lead["instruction"] and "retries forever" in lead["instruction"]


def test_overlap_merges_transitively(tmp_path: Path) -> None:
    td = _setup(tmp_path, [_single_entry("code-quality-auditor")], stage="REPAIR_PLANNING")
    _project(td)
    _report(td, "code-quality-auditor", DEEP, [
        {"id": "CQ-1", "severity": "minor", "blocking": True, "location": "src/a.py:1"},
        {"id": "CQ-2", "severity": "minor", "blocking": True, "location": "src/auth.py:1"},
        {"id": "CQ-3", "severity": "minor", "blocking": True, "location": "src/a.py:2, src/auth.py:2"},
    ])

    batches = _build(td, tmp_path)

    assert [len(b["tasks"]) for b in batches] == [1]
    assert sorted(batches[0]["tasks"][0]["files_to_modify"]) == ["src/a.py", "src/auth.py"]


def test_fileless_finding_scopes_to_the_segment_delivering_its_criterion(tmp_path: Path) -> None:
    td = _setup(tmp_path, [_single_entry("spec-completion-auditor")], stage="REPAIR_PLANNING")
    _project(td)
    (td / "execution-graph.json").write_text(json.dumps({"segments": [
        {"id": "seg-1", "executor": "backend-executor", "files_expected": ["src/auth.py", "src/a.py"],
         "criteria_ids": [1]},
        {"id": "seg-2", "executor": "ui-executor", "files_expected": ["web/app.tsx"], "criteria_ids": [2]},
    ]}))
    _report(td, "spec-completion-auditor", DEEP, [
        {"id": "SC-1", "severity": "critical", "blocking": True, "location": "",
         "description": "AC 2 is not met: the empty state never renders."},
        {"id": "SC-2", "severity": "critical", "blocking": True, "location": "web/app.tsx:9"},
    ])

    batches = _build(td, tmp_path)

    by_id = {t["finding_id"]: (i, t) for i, b in enumerate(batches) for t in b["tasks"]}
    idx, inferred = by_id["SC-1"]
    assert inferred["files_to_modify"] == ["web/app.tsx"]
    assert inferred["files_inferred"] is True
    # A guessed scope is never merged into a cited one; it runs after it.
    assert "merged_findings" not in by_id["SC-2"][1]
    assert idx > by_id["SC-2"][0]


# ---------------------------------------------------------------------------
# 3. Data Model gap analysis
# ---------------------------------------------------------------------------

def test_model_scan_skips_virtualenvs_by_name_and_by_marker(tmp_path: Path) -> None:
    for venv in (".venv", "env"):
        site = tmp_path / "backend" / venv / "lib" / "site-packages"
        site.mkdir(parents=True)
        (tmp_path / "backend" / venv / "pyvenv.cfg").write_text("home = /usr\n")
        for i in range(30):
            (site / f"m{i}.py").write_text("x = 1\n")
    models = tmp_path / "backend" / "app" / "models.py"
    models.parent.mkdir(parents=True)
    models.write_text("class Organization(Base):\n    __tablename__ = 'orgs'\n")

    files = _iter_code_files(tmp_path, {".py"}, limit=5)

    assert files == [models]


def test_data_model_reads_annotated_cells_and_table_names(tmp_path: Path) -> None:
    models = tmp_path / "backend" / "app" / "models.py"
    models.parent.mkdir(parents=True)
    models.write_text(
        "class Organization(Base):\n    __tablename__ = 'orgs'\n\n"
        "class Membership(models.Model):\n    class Meta:\n        db_table = 'org_members'\n"
    )
    plan = (
        "# Plan\n\n## Data Model\n\n| Model | Notes |\n|---|---|\n"
        "| `Organization` (existing, app/models.py) | |\n"
        "| models.Membership | existing |\n"
        "| orgs | table |\n"
        "| `org_members` | table |\n"
        "| Invite | to-be-created |\n"
    )

    result = analyze_data_model(plan, tmp_path)

    assert result["unverified"] == []
    assert result["verified"] == ["membership", "org_members", "organization", "orgs"]
    assert result["to_be_created"] == ["invite"]


# ---------------------------------------------------------------------------
# 6. --tokens-used None
# ---------------------------------------------------------------------------

def test_tokens_used_accepts_the_skills_none_placeholder() -> None:
    parser = ctl.build_parser()
    args = parser.parse_args([
        "run-execution-segment-done", ".dynos/task-x", "seg-1",
        "--injected-prompt-sha256", "abc", "--tokens-used", "None",
    ])
    assert args.tokens_used == 0
    args = parser.parse_args(["tdd-receipt", ".dynos/task-x", "--tokens-used", "1200"])
    assert args.tokens_used == 1200
