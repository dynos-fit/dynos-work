"""
Regression tests: the dossier must carry the code a bug report names.

A real investigation named 8 files and ~25 functions, yet the dossier minted a
single F entry ("include", a junk token) and thousands of repo-wide linter
findings — no evidence ID carried any named function's source, so the
investigator could only answer INSUFFICIENT_EVIDENCE. These tests build a
dossier for a small fixture repo and assert that files, line-ref snippets and
symbol definitions are minted, that lint noise is scoped to the named files,
and that `triage.py finalize` accepts citations of the new evidence.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

_DEBUG_MODULE_DIR = Path(__file__).resolve().parent.parent
if str(_DEBUG_MODULE_DIR) not in sys.path:
    sys.path.insert(0, str(_DEBUG_MODULE_DIR))

import triage
from debuglib import bug_classifier, code_evidence, git_forensics

TRIAGE_SCRIPT = str(_DEBUG_MODULE_DIR / "triage.py")
SCHEMA_PATH = _DEBUG_MODULE_DIR / "schemas" / "evidence_dossier.schema.json"


CTL_SRC = '''"""Fixture control module."""
import os

MAX_RETRIES = 3


def cmd_clear_role(args):
    """Remove the shared role marker."""
    marker = os.path.join(args.task_dir, "active-segment-role")
    if os.path.exists(marker):
        os.unlink(marker)
    return 0


def _merge_overlapping_repair_tasks(tasks):
    merged = []
    for t in tasks:
        merged.append(t)
    return merged


class Router:
    def route(self, finding):
        return finding.get("executor")
'''

QLEARN_SRC = '''def build_repair_plan(findings, epsilon=0.1):
    plan = []
    for f in findings:
        plan.append({"finding": f, "executor": "refactor-executor"})
    return plan
'''

UNRELATED_SRC = '''def helper_unrelated():
    return 1


def normalize_repair_severity(sev):
    if sev in ("major", "blocking"):
        return "high"
    return sev
'''


@pytest.fixture()
def fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "hooks").mkdir(parents=True)
    (repo / "memory").mkdir()
    (repo / "lib").mkdir()
    (repo / "hooks" / "ctl.py").write_text(CTL_SRC)
    (repo / "memory" / "lib_qlearn.py").write_text(QLEARN_SRC)
    (repo / "lib" / "validate.py").write_text(UNRELATED_SRC)
    return repo


BUG_TEXT = (
    "Repair routing is wrong. hooks/ctl.py run-repair takes the executor from "
    "build_repair_plan in memory/lib_qlearn.py. `ctl clear-role` "
    "(cmd_clear_role) unlinks the marker; see hooks/ctl.py:10 and "
    "memory/lib_qlearn.py:2-4. _merge_overlapping_repair_tasks keeps the lead "
    "executor, Router.route ignores file type, normalize_repair_severity maps "
    "blocking to high, and MAX_RETRIES is per finding. Django routes only "
    "follow string include(), not include(variable)."
)


def _payloads(dossier: dict, kind: str) -> list[dict]:
    index = dossier["evidence_index"]
    return [index[eid]["payload"] for eid in dossier["evidence_ids_by_kind"].get(kind, [])]


def _run(repo: Path, monkeypatch: pytest.MonkeyPatch, linter=None, semgrep=None) -> dict:
    monkeypatch.setattr(triage.run_linters, "run", lambda *a, **k: list(linter or []))
    monkeypatch.setattr(triage.run_semgrep, "run", lambda *a, **k: list(semgrep or []))
    monkeypatch.setattr(triage.coverage_gaps, "find_gaps", lambda *a, **k: [])
    monkeypatch.setattr(triage.log_surface, "surface", lambda *a, **k: [])
    return triage.run_pipeline(BUG_TEXT, str(repo), None, False)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def test_classifier_extracts_identifiers_and_line_ranges():
    result = bug_classifier.classify(
        "see hooks/ctl.py:120-140 and Main.java:34:5; `Foo.bar()` calls "
        "doThing() and build_repair_plan; QLEARN_EPSILON in lib_defaults.py; "
        "pkg.module.func is involved"
    )
    files = {f["path"]: f for f in result["mentioned_files"]}
    assert files["hooks/ctl.py"]["line"] == 120
    assert files["hooks/ctl.py"]["end_line"] == 140
    assert files["Main.java"]["line"] == 34 and files["Main.java"]["col"] == 5
    syms = result["mentioned_symbols"]
    for expected in ("Foo.bar", "doThing", "build_repair_plan", "QLEARN_EPSILON",
                     "pkg.module.func"):
        assert expected in syms, syms
    # File names are not double-counted as symbols.
    assert "lib_defaults" not in syms
    assert "ctl.py" not in syms


def test_junk_tokens_do_not_become_file_evidence(fixture_repo, monkeypatch):
    dossier = _run(fixture_repo, monkeypatch)
    for payload in _payloads(dossier, "F"):
        assert isinstance(payload, dict), payload
        assert payload["path"] in {"hooks/ctl.py", "memory/lib_qlearn.py"}
    assert "include" not in {p["symbol"] for p in _payloads(dossier, "SY")}
    assert "include" in dossier["evidence_scope"]["code_evidence"]["symbols_unresolved"]


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------

def test_mentioned_files_minted_with_metadata_and_head(fixture_repo, monkeypatch):
    dossier = _run(fixture_repo, monkeypatch)
    files = [p for p in _payloads(dossier, "F") if p["type"] == "file"]
    by_path = {p["path"]: p for p in files}
    assert set(by_path) == {"hooks/ctl.py", "memory/lib_qlearn.py"}
    ctl = by_path["hooks/ctl.py"]
    assert ctl["line_count"] == len(CTL_SRC.splitlines())
    assert ctl["size_bytes"] == len(CTL_SRC.encode())
    assert "Fixture control module" in ctl["head"]["text"]


def test_line_references_produce_snippets(fixture_repo, monkeypatch):
    dossier = _run(fixture_repo, monkeypatch)
    snippets = [p for p in _payloads(dossier, "F") if p["type"] == "snippet"]
    by_path = {p["path"]: p for p in snippets}
    ctl = by_path["hooks/ctl.py"]
    assert ctl["referenced_line"] == 10
    assert ctl["start_line"] <= 10 <= ctl["end_line"]
    assert "os.path.join(args.task_dir" in ctl["source"]
    ql = by_path["memory/lib_qlearn.py"]
    assert (ql["start_line"], ql["end_line"]) == (2, 4)
    assert "plan = []" in ql["source"]
    assert "def build_repair_plan" not in ql["source"]  # line 1 is outside 2-4


def test_symbol_definitions_minted_with_source(fixture_repo, monkeypatch):
    dossier = _run(fixture_repo, monkeypatch)
    sy = {p["symbol"]: p for p in _payloads(dossier, "SY")}

    clear = sy["cmd_clear_role"]
    assert clear["path"] == "hooks/ctl.py"
    assert clear["kind"] == "function"
    assert (clear["start_line"], clear["end_line"]) == (7, 12)
    assert "os.unlink(marker)" in clear["source"]
    assert re.search(r"^\s*7\| def cmd_clear_role", clear["source"], re.MULTILINE)

    plan = sy["build_repair_plan"]
    assert plan["path"] == "memory/lib_qlearn.py"
    assert "refactor-executor" in plan["source"]

    assert sy["_merge_overlapping_repair_tasks"]["path"] == "hooks/ctl.py"
    assert sy["Router.route"]["kind"] == "method"
    assert sy["MAX_RETRIES"]["kind"] == "assignment"

    # Distinctive name defined outside the mentioned files: found repo-wide.
    norm = sy["normalize_repair_severity"]
    assert norm["path"] == "lib/validate.py"
    assert norm["resolved_in"] == "repo"
    assert '"high"' in norm["source"]


def test_symbol_source_is_bounded(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    body = "\n".join(f"    x{i} = {i}" for i in range(400))
    (repo / "big.py").write_text(f"def huge_function_name():\n{body}\n    return 0\n")
    result = code_evidence.collect(str(repo), [{"path": "big.py"}], ["huge_function_name"])
    entry = result["symbols"][0]
    assert entry["end_line"] == 402
    assert entry["source_truncated"] is True
    assert len(entry["source"].splitlines()) == code_evidence.MAX_SYMBOL_LINES


def test_kebab_subcommand_resolves_cmd_handler(fixture_repo):
    result = code_evidence.collect(
        str(fixture_repo), [{"path": "hooks/ctl.py"}], [], "run ctl clear-role now",
    )
    entries = [s for s in result["symbols"] if s["symbol"] == "cmd_clear_role"]
    assert entries and entries[0]["derived_from"] == "clear-role"


# ---------------------------------------------------------------------------
# Scoping
# ---------------------------------------------------------------------------

def test_linter_and_semgrep_findings_scoped_to_mentioned_files(fixture_repo, monkeypatch):
    linter = [
        {"tool": "ruff", "file": str(fixture_repo / "hooks" / "ctl.py"), "line": 3,
         "severity": "warning", "message": "in scope"},
        {"tool": "ruff", "file": "lib/validate.py", "line": 1,
         "severity": "warning", "message": "out of scope"},
        {"tool": "eslint", "skipped": True, "reason": "not installed"},
    ]
    semgrep = [
        {"rule_id": "r", "file": "./memory/lib_qlearn.py", "line": 2, "message": "m", "severity": "INFO"},
        {"rule_id": "r", "file": "lib/validate.py", "line": 2, "message": "m", "severity": "INFO"},
    ]
    dossier = _run(fixture_repo, monkeypatch, linter=linter, semgrep=semgrep)
    mf = _payloads(dossier, "MF")
    assert [p.get("message") for p in mf if not p.get("skipped")] == ["in scope"]
    assert any(p.get("skipped") for p in mf)
    s = _payloads(dossier, "S")
    assert [p["file"] for p in s] == ["./memory/lib_qlearn.py"]
    scope = dossier["evidence_scope"]
    assert scope["linter_findings"]["dropped_outside_mentioned_files"] == 1
    assert scope["semgrep_findings"]["dropped_outside_mentioned_files"] == 1


def test_findings_capped_when_no_files_mentioned(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    linter = [
        {"tool": "ruff", "file": f"f{i}.py", "line": 1, "severity": "w", "message": "x"}
        for i in range(triage._UNSCOPED_FINDINGS_CAP + 50)
    ]
    monkeypatch.setattr(triage.run_linters, "run", lambda *a, **k: list(linter))
    monkeypatch.setattr(triage.run_semgrep, "run", lambda *a, **k: [])
    monkeypatch.setattr(triage.coverage_gaps, "find_gaps", lambda *a, **k: [])
    monkeypatch.setattr(triage.log_surface, "surface", lambda *a, **k: [])
    dossier = triage.run_pipeline("the app crashes sometimes", str(repo), None, False)
    assert len(dossier["evidence_ids_by_kind"]["MF"]) == triage._UNSCOPED_FINDINGS_CAP
    assert dossier["evidence_scope"]["linter_findings"]["dropped_over_cap"] == 50


# ---------------------------------------------------------------------------
# Schema + finalize
# ---------------------------------------------------------------------------

def test_schema_accepts_new_kind_and_wide_counters():
    schema = json.loads(SCHEMA_PATH.read_text())
    (pattern,) = schema["properties"]["evidence_index"]["patternProperties"].keys()
    for eid in ("SY-001", "F-001", "MF-2348", "S-001", "CG-010"):
        assert re.match(pattern, eid), eid
    assert not re.match(pattern, "X-001")


def test_blame_is_coalesced_into_ranges():
    recs = [
        {"commit": "a", "author": "x", "date": "d", "line_no": 1},
        {"commit": "a", "author": "x", "date": "d", "line_no": 2},
        {"commit": "b", "author": "y", "date": "e", "line_no": 3},
        {"commit": "a", "author": "x", "date": "d", "line_no": 4},
    ]
    ranges = git_forensics._coalesce_blame(recs)
    assert [(r["start_line"], r["end_line"], r["commit"]) for r in ranges] == [
        (1, 2, "a"), (3, 3, "b"), (4, 4, "a"),
    ]


def test_finalize_accepts_citations_of_file_and_symbol_evidence(fixture_repo, tmp_path):
    out_dir = fixture_repo / ".dynos" / "investigations" / "t1"
    dossier_path = out_dir / "dossier.json"
    proc = subprocess.run(
        [sys.executable, TRIAGE_SCRIPT, "--bug", BUG_TEXT, "--repo", str(fixture_repo),
         "--out", str(dossier_path)],
        capture_output=True, text=True, timeout=300, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    dossier = json.loads(dossier_path.read_text())
    sy = {dossier["evidence_index"][eid]["payload"]["symbol"]: eid
          for eid in dossier["evidence_ids_by_kind"]["SY"]}
    f_ids = dossier["evidence_ids_by_kind"]["F"]
    assert "cmd_clear_role" in sy and "build_repair_plan" in sy and f_ids

    report = {
        "investigation_id": dossier["investigation_id"],
        "causal_chain": [
            {"step": 1, "description": "clear-role unlinks the shared marker",
             "evidence_ids": [sy["cmd_clear_role"], f_ids[0]]},
        ],
        "root_cause": {"description": "shared marker", "evidence_ids": [sy["cmd_clear_role"]]},
        "recommended_fix": {
            "description": "scope the marker per executor",
            "locations": [{"file": "hooks/ctl.py", "line": 7}],
            "evidence_ids": [sy["build_repair_plan"]],
        },
    }
    fin = subprocess.run(
        [sys.executable, TRIAGE_SCRIPT, "finalize", "--report", "-", "--dossier", str(dossier_path)],
        input=json.dumps(report), capture_output=True, text=True, timeout=60, check=False,
    )
    assert fin.returncode == 0, fin.stderr
    assert (out_dir / "report.md").exists()

    report["root_cause"]["evidence_ids"] = ["SY-999"]
    bad = subprocess.run(
        [sys.executable, TRIAGE_SCRIPT, "finalize", "--report", "-", "--dossier", str(dossier_path)],
        input=json.dumps(report), capture_output=True, text=True, timeout=60, check=False,
    )
    assert bad.returncode == 1
    assert "SY-999" in bad.stderr
