"""Tests for ``memory/postmortem_analysis.py backfill-rejected``.

Walks every ``.dynos/task-*/postmortem-analysis.json`` and merges rules
that were dropped by the original "missing template" check (since fixed
in PR #163 to default to ``"advisory"``) into the persistent
``prevention-rules.json``. The recent PRO-001/005/007 residual drains
each had 2-8 rules sitting unmerged for this exact reason.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
MEMORY_DIR = REPO_ROOT / "memory"
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(MEMORY_DIR))


def _make_task_with_analysis(root: Path, task_id: str, rules: list[dict]) -> None:
    task_dir = root / ".dynos" / task_id
    task_dir.mkdir(parents=True)
    (task_dir / "postmortem-analysis.json").write_text(
        json.dumps({"task_id": task_id, "prevention_rules": rules})
    )


def _run_backfill(root: Path, dry_run: bool = False) -> dict:
    """Invoke cmd_backfill_rejected via in-process import."""
    import argparse
    import io
    import contextlib

    from postmortem_analysis import cmd_backfill_rejected

    args = argparse.Namespace(root=str(root), dry_run=dry_run)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_backfill_rejected(args)
    assert rc == 0
    return json.loads(buf.getvalue())


def test_backfill_salvages_untemplated_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Rules with non-empty ``rule`` text but no ``template`` field
    must be merged with template defaulted to ``"advisory"``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    _make_task_with_analysis(tmp_path, "task-A", [
        {
            "executor": "all",
            "category": "process",
            "rule": "Always do X before Y.",
            "rationale": "Y depends on X.",
        },
        {
            "executor": "testing-executor",
            "category": "test",
            "rule": "Tests for forbidden patterns must walk all ast.List nodes.",
            "rationale": "Helper-mediated patterns silently bypass narrower scans.",
        },
    ])

    result = _run_backfill(tmp_path, dry_run=False)
    assert result["scanned_tasks"] == 1
    assert result["candidate_rules"] == 2
    assert result["novel_rules"] == 2

    rules_path = Path(result["rules_path"])
    assert rules_path.is_file()
    doc = json.loads(rules_path.read_text())
    rules = doc["rules"]
    assert len(rules) == 2
    for r in rules:
        assert r["template"] == "advisory", "salvaged rules must carry the default template"
        assert r["source_task"] == "task-A", "source_task should be set on backfilled rules"


def test_backfill_skips_rules_with_existing_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Rules that already have a ``template`` were accepted on the
    original apply pass — backfill must not double-merge them."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    _make_task_with_analysis(tmp_path, "task-B", [
        {
            "executor": "all",
            "category": "process",
            "rule": "Already-accepted rule.",
            "template": "advisory",
            "params": {},
        },
        {
            "executor": "all",
            "category": "process",
            "rule": "Untemplated rule that is salvageable.",
        },
    ])

    result = _run_backfill(tmp_path, dry_run=False)
    assert result["candidate_rules"] == 1, (
        "templated rules must not appear as candidates"
    )
    assert result["novel_rules"] == 1


def test_backfill_skips_rules_with_no_rule_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Rules with empty/missing ``rule`` text don't satisfy the
    default-to-advisory heuristic and must be skipped (the original
    schema rejection was correct for them)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    _make_task_with_analysis(tmp_path, "task-C", [
        {"executor": "all", "category": "process"},
        {"executor": "all", "rule": "   "},
        {"executor": "all", "rule": "valid rule text"},
    ])

    result = _run_backfill(tmp_path, dry_run=False)
    assert result["candidate_rules"] == 1
    assert result["novel_rules"] == 1


def test_backfill_dedups_against_existing_prevention_rules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A salvageable rule whose (executor, category, rule[:200]) key
    already appears in prevention-rules.json must be skipped."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    # Run first backfill to seed prevention-rules.json.
    _make_task_with_analysis(tmp_path, "task-D1", [
        {
            "executor": "backend-executor",
            "category": "sec",
            "rule": "Pin python3 binaries via shutil.which().",
        },
    ])
    first = _run_backfill(tmp_path, dry_run=False)
    assert first["novel_rules"] == 1

    # Add the same rule under a new task — second backfill must skip.
    _make_task_with_analysis(tmp_path, "task-D2", [
        {
            "executor": "backend-executor",
            "category": "sec",
            "rule": "Pin python3 binaries via shutil.which().",
        },
    ])
    second = _run_backfill(tmp_path, dry_run=False)
    assert second["candidate_rules"] == 2  # both tasks contribute
    assert second["novel_rules"] == 0, (
        "duplicate (executor, category, rule) must not double-merge"
    )


def test_backfill_dry_run_does_not_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``--dry-run`` reports the counts but never touches the file."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    _make_task_with_analysis(tmp_path, "task-E", [
        {"executor": "all", "category": "process", "rule": "salvageable rule"},
    ])

    result = _run_backfill(tmp_path, dry_run=True)
    assert result["dry_run"] is True
    assert result["novel_rules"] == 1
    rules_path = Path(result["rules_path"])
    assert not rules_path.exists(), "dry-run must not create the rules file"


def test_backfill_handles_missing_or_malformed_analysis_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Tasks without postmortem-analysis.json, or with malformed JSON,
    must be skipped silently (they don't tell us anything about
    salvageable rules)."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    # Task with no postmortem-analysis.json
    (tmp_path / ".dynos" / "task-noanalysis").mkdir(parents=True)
    # Task with malformed JSON
    bad_dir = tmp_path / ".dynos" / "task-bad"
    bad_dir.mkdir(parents=True)
    (bad_dir / "postmortem-analysis.json").write_text("not json {")
    # Task with valid analysis
    _make_task_with_analysis(tmp_path, "task-good", [
        {"executor": "all", "rule": "real salvageable rule"},
    ])

    result = _run_backfill(tmp_path, dry_run=False)
    assert result["scanned_tasks"] == 3
    assert result["novel_rules"] == 1


def test_backfill_handles_existing_rules_doc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Pre-existing prevention-rules.json must be preserved verbatim.
    Backfill appends; it never overwrites."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    # Compute the rules path manually (matches _persistent_project_dir logic).
    from lib_core import _persistent_project_dir
    persistent = _persistent_project_dir(tmp_path)
    persistent.mkdir(parents=True, exist_ok=True)
    rules_path = persistent / "prevention-rules.json"

    pre_existing = {
        "rules": [
            {
                "executor": "all",
                "category": "process",
                "rule": "pre-existing rule from earlier work",
                "template": "advisory",
                "params": {},
            }
        ],
        "extra_field": "must be preserved",
    }
    rules_path.write_text(json.dumps(pre_existing))

    _make_task_with_analysis(tmp_path, "task-mix", [
        {"executor": "testing-executor", "rule": "new rule from backfill"},
    ])

    _run_backfill(tmp_path, dry_run=False)

    final = json.loads(rules_path.read_text())
    assert len(final["rules"]) == 2
    # Original rule preserved
    assert any(r["rule"] == "pre-existing rule from earlier work" for r in final["rules"])
    # New rule appended
    assert any(r["rule"] == "new rule from backfill" for r in final["rules"])
    # Top-level extra fields preserved
    assert final["extra_field"] == "must be preserved"


def test_backfill_writes_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """``cmd_backfill_rejected`` must persist ``prevention-rules.json``
    via the atomic ``write_json`` helper (tempfile + fsync + os.rename),
    not via a raw ``write_text(json.dumps(...))`` call.

    This test pins the observable difference between the two write
    paths: ``write_json`` appends a trailing ``\\n`` (see
    ``hooks/lib_core.py:166``), while ``write_text(json.dumps(...))``
    does not. A single-byte tell that catches a regression of line
    1092 back to the non-atomic write.

    Covers ACs 3, 4, 5, 6 — and structurally guards AC 7 by ensuring
    the only call site writing ``rules_path`` is the atomic helper.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(fake_home / ".dynos"))

    # Seed an existing rules doc so the test exercises the merge-and-write
    # path (existing_doc has both a prior rule and the newly added one).
    from lib_core import _persistent_project_dir
    persistent = _persistent_project_dir(tmp_path)
    persistent.mkdir(parents=True, exist_ok=True)
    rules_path_seed = persistent / "prevention-rules.json"
    seed_doc = {
        "rules": [
            {
                "executor": "all",
                "category": "process",
                "rule": "seed rule for atomic write regression",
                "template": "advisory",
                "params": {},
            }
        ],
    }
    rules_path_seed.write_text(json.dumps(seed_doc))

    _make_task_with_analysis(tmp_path, "task-atomic", [
        {
            "executor": "all",
            "category": "process",
            "rule": "atomic write regression rule (novel)",
        },
    ])

    result = _run_backfill(tmp_path, dry_run=False)
    assert result["novel_rules"] == 1
    rules_path = Path(result["rules_path"])
    assert rules_path == rules_path_seed
    assert rules_path.is_file()

    # AC 3 + AC 4: trailing newline byte — the strongest signal that
    # ``write_json`` (not ``write_text``) was used at line 1092.
    raw = rules_path.read_bytes()
    assert raw.endswith(b"\n"), (
        "prevention-rules.json must end with a trailing newline, which is "
        "the documented behavior of write_json (lib_core.py:166). A missing "
        "newline indicates the non-atomic write_text path is still in use."
    )

    # AC 5: file is parseable as JSON (write_json's append of '\n' is
    # whitespace, which json.loads tolerates).
    content = json.loads(rules_path.read_text())  # must not raise

    # AC 6: parsed content matches the in-memory doc that was written —
    # seed rule preserved, novel rule appended.
    assert isinstance(content, dict)
    assert "rules" in content
    assert len(content["rules"]) == 2, (
        "expected seed rule + 1 novel rule = 2 total"
    )
    rule_texts = {r["rule"] for r in content["rules"]}
    assert "seed rule for atomic write regression" in rule_texts
    assert "atomic write regression rule (novel)" in rule_texts

    # Structural guard for AC 7: write_json removes its tempfile via
    # os.rename on success and unlinks on failure, so no orphan
    # ``.tmp`` siblings should remain in the rules dir after a clean run.
    leftover_tmps = [
        p for p in rules_path.parent.iterdir()
        if p.name != rules_path.name and p.suffix == ".tmp"
    ]
    assert leftover_tmps == [], (
        f"unexpected tempfile leftovers in {rules_path.parent}: {leftover_tmps}"
    )
