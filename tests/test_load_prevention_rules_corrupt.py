"""Tests for load_prevention_rules absent vs corrupt distinction (AC 17).

Absent rules file → return [] (no event).
Corrupt JSON → emit prevention_rules_corrupt event AND raise.
Non-dict top-level → emit event AND raise ValueError.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from router import load_prevention_rules  # noqa: E402


def _setup(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dynos").mkdir()
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos-home"))
    return root


def _events(root: Path) -> list[dict]:
    events_path = root / ".dynos" / "events.jsonl"
    if not events_path.exists():
        return []
    return [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]


def test_absent_file_returns_empty_list_no_event(tmp_path: Path, monkeypatch):
    """AC 17: absent prevention-rules.json → return [] silently."""
    root = _setup(tmp_path, monkeypatch)
    assert load_prevention_rules(root) == []
    # No event should have been logged.
    events = _events(root)
    corrupt = [e for e in events if e.get("event") == "prevention_rules_corrupt"]
    assert corrupt == []


def test_valid_rules_returns_rules_list(tmp_path: Path, monkeypatch):
    """AC 17 control: well-formed file → returns the rules list."""
    root = _setup(tmp_path, monkeypatch)
    from lib_core import ensure_persistent_project_dir
    pd = ensure_persistent_project_dir(root)
    (pd / "prevention-rules.json").write_text(json.dumps({
        "rules": [{"id": "r1", "rule": "test rule"}],
    }))
    assert load_prevention_rules(root) == [{"id": "r1", "rule": "test rule"}]


def test_corrupt_json_raises_and_emits_event(tmp_path: Path, monkeypatch):
    """AC 17: malformed JSON → log prevention_rules_corrupt + raise."""
    root = _setup(tmp_path, monkeypatch)
    from lib_core import ensure_persistent_project_dir
    pd = ensure_persistent_project_dir(root)
    (pd / "prevention-rules.json").write_text("not json {{{ malformed")

    with pytest.raises((json.JSONDecodeError, ValueError)):
        load_prevention_rules(root)

    events = _events(root)
    corrupt = [e for e in events if e.get("event") == "prevention_rules_corrupt"]
    assert len(corrupt) == 1
    assert "prevention-rules.json" in corrupt[0].get("path", "")


def test_non_dict_top_level_raises(tmp_path: Path, monkeypatch):
    """AC 17: bare list at top-level is not a valid rules file → raise ValueError."""
    root = _setup(tmp_path, monkeypatch)
    from lib_core import ensure_persistent_project_dir
    pd = ensure_persistent_project_dir(root)
    (pd / "prevention-rules.json").write_text(json.dumps([1, 2, 3]))

    with pytest.raises(ValueError):
        load_prevention_rules(root)

    events = _events(root)
    corrupt = [e for e in events if e.get("event") == "prevention_rules_corrupt"]
    assert len(corrupt) == 1


def test_dict_without_rules_key_returns_empty_list(tmp_path: Path, monkeypatch):
    """AC 17: well-formed dict but no 'rules' key → return [] (no rules)."""
    root = _setup(tmp_path, monkeypatch)
    from lib_core import ensure_persistent_project_dir
    pd = ensure_persistent_project_dir(root)
    (pd / "prevention-rules.json").write_text(json.dumps({"other_key": "value"}))
    assert load_prevention_rules(root) == []
