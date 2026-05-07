"""Tests for _generate_rule_id and apply_analysis rule_id emission.

Covers ACs 1, 2, 3, 17 from task-20260507-004.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the memory module is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


# ---------------------------------------------------------------------------
# AC 1: _generate_rule_id helper
# ---------------------------------------------------------------------------


def test_generate_rule_id_deterministic():
    """AC 1: _generate_rule_id returns byte-identical output for identical inputs.

    Two separate in-process calls with the same args must return the same string.
    """
    from postmortem_analysis import _generate_rule_id  # noqa: PLC0415

    result_a = _generate_rule_id("same text", "perf")
    result_b = _generate_rule_id("same text", "perf")
    assert result_a == result_b, (
        f"_generate_rule_id is not deterministic: got {result_a!r} then {result_b!r}"
    )


def test_generate_rule_id_format():
    """AC 1: output must match regex ^[a-z0-9]{1,4}-[0-9a-f]{12}$."""
    from postmortem_analysis import _generate_rule_id  # noqa: PLC0415

    result = _generate_rule_id("some rule", "security")
    pattern = re.compile(r"^[a-z0-9]{1,4}-[0-9a-f]{12}$")
    assert pattern.fullmatch(result), (
        f"_generate_rule_id output {result!r} does not match ^[a-z0-9]{{1,4}}-[0-9a-f]{{12}}$"
    )


def test_generate_rule_id_no_collisions_200_fuzz():
    """AC 1: 200 distinct rule texts must produce 200 distinct rule IDs (no collisions)."""
    from postmortem_analysis import _generate_rule_id  # noqa: PLC0415

    texts = [f"rule text variant {i}" for i in range(200)]
    ids = [_generate_rule_id(text, "test") for text in texts]
    unique_ids = set(ids)
    assert len(unique_ids) == 200, (
        f"Expected 200 unique rule_ids but got {len(unique_ids)} "
        f"(collision among 200 variants)"
    )


def test_generate_rule_id_differs_on_single_char_change():
    """AC 1: a single character difference in rule_text must produce a different rule_id."""
    from postmortem_analysis import _generate_rule_id  # noqa: PLC0415

    id_a = _generate_rule_id("rule text A", "category")
    id_b = _generate_rule_id("rule text B", "category")
    assert id_a != id_b, (
        f"Single char change did not change rule_id: both returned {id_a!r}"
    )


def test_generate_rule_id_prefix_from_category():
    """AC 1: prefix is the first 4 chars of category.lower()."""
    from postmortem_analysis import _generate_rule_id  # noqa: PLC0415

    result = _generate_rule_id("any rule text", "SECURITY")
    assert result.startswith("secu-"), (
        f"Expected prefix 'secu-' from category 'SECURITY', got {result!r}"
    )


def test_generate_rule_id_empty_category_fallback():
    """AC 1: empty category uses 'unkn' fallback to match regex ^[a-z0-9]{1,4}-...$."""
    from postmortem_analysis import _generate_rule_id  # noqa: PLC0415

    result = _generate_rule_id("some rule", "")
    pattern = re.compile(r"^[a-z0-9]{1,4}-[0-9a-f]{12}$")
    assert pattern.fullmatch(result), (
        f"Empty category result {result!r} does not match regex"
    )
    assert result.startswith("unkn-"), (
        f"Empty category should use 'unkn' fallback, got {result!r}"
    )


def test_generate_rule_id_exact_sha256():
    """AC 1: verify the hash portion is the first 12 hex chars of SHA-256 of rule_text.encode('utf-8')."""
    from postmortem_analysis import _generate_rule_id  # noqa: PLC0415

    rule_text = "do not call sys.exit"
    category = "safe"
    expected_digest = hashlib.sha256(rule_text.encode("utf-8")).hexdigest()[:12]
    expected = f"safe-{expected_digest}"
    result = _generate_rule_id(rule_text, category)
    assert result == expected, f"Expected {expected!r}, got {result!r}"


# ---------------------------------------------------------------------------
# AC 2: apply_analysis emits rule_id on every rule
# ---------------------------------------------------------------------------


def _make_task_dir(tmp_path: Path) -> Path:
    """Create a minimal task directory structure."""
    project_root = tmp_path / "project"
    task_dir = project_root / ".dynos" / "task-test-001"
    task_dir.mkdir(parents=True)
    # Write a minimal retrospective so apply_analysis can read task_id
    retro = {"task_id": "task-test-001", "stage": "complete"}
    (task_dir / "task-retrospective.json").write_text(json.dumps(retro))
    return task_dir


def test_apply_analysis_emits_rule_id(tmp_path: Path, monkeypatch):
    """AC 2: apply_analysis writes rule_id on newly added rules.

    After calling apply_analysis with a synthetic finding, the written rule
    must pass _rule_from_dict (i.e., have a valid rule_id and template).
    """
    from postmortem_analysis import apply_analysis  # noqa: PLC0415
    from rules_engine import _rule_from_dict  # noqa: PLC0415

    task_dir = _make_task_dir(tmp_path)
    project_root = task_dir.parent.parent

    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos_home"))

    analysis = {
        "prevention_rules": [
            {
                "rule": "Do not use eval() in production code",
                "category": "security",
                "template": "advisory",
                "enforcement": "prompt-constraint",
                "rationale": "eval is dangerous",
                "source_finding": "f-001",
                "executor": "all",
            }
        ]
    }

    result = apply_analysis(task_dir, analysis)

    # Locate the written prevention-rules.json
    persistent_dir = tmp_path / "dynos_home" / "projects"
    # Find the rules file — search within persistent dirs
    rule_files = list((tmp_path / "dynos_home").rglob("prevention-rules.json"))
    assert rule_files, "apply_analysis must write prevention-rules.json"

    rules_data = json.loads(rule_files[0].read_text())
    rules_list = rules_data.get("rules", [])
    assert rules_list, "No rules were written"

    # The newly written rule must have rule_id
    new_rule = rules_list[-1]
    assert "rule_id" in new_rule, (
        f"apply_analysis did not emit rule_id on rule: {new_rule}"
    )

    # Must pass _rule_from_dict (loadable by the engine)
    loaded = _rule_from_dict(new_rule)
    assert loaded is not None, (
        f"apply_analysis wrote a rule that _rule_from_dict cannot load: {new_rule}"
    )


# ---------------------------------------------------------------------------
# AC 3: apply_analysis rejects empty rule text with ValueError
# ---------------------------------------------------------------------------


def test_apply_analysis_rejects_empty_rule(tmp_path: Path, monkeypatch):
    """AC 3: apply_analysis raises ValueError when rule text is empty after migration.

    A rule entry with rule=="" and no legacy_text must raise ValueError with
    a message containing "empty rule text".
    """
    from postmortem_analysis import apply_analysis  # noqa: PLC0415

    task_dir = _make_task_dir(tmp_path)
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos_home"))

    analysis = {
        "prevention_rules": [
            {
                "rule": "",
                "category": "quality",
                "template": "advisory",
                "enforcement": "prompt-constraint",
                "rationale": "this should be rejected",
                "source_finding": "f-002",
                "executor": "all",
            }
        ]
    }

    with pytest.raises(ValueError, match="empty rule text"):
        apply_analysis(task_dir, analysis)


# ---------------------------------------------------------------------------
# AC 17: size cap trims oldest entries and emits events
# ---------------------------------------------------------------------------


def test_apply_analysis_size_cap_trims(tmp_path: Path, monkeypatch):
    """AC 17: after append, if len(rules) > MAX_PREVENTION_RULES, oldest entries are trimmed.

    Seeds a registry with MAX_PREVENTION_RULES entries, calls apply_analysis
    with one new rule, asserts the result has exactly MAX_PREVENTION_RULES entries.
    """
    import postmortem_analysis as pma  # noqa: PLC0415

    task_dir = _make_task_dir(tmp_path)
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos_home"))

    # Build the persistent dir and seed the registry at cap
    from hooks_path_helper import get_persistent_dir  # noqa: PLC0415 - may not exist
    # Use DYNOS_HOME-based resolution
    home = tmp_path / "dynos_home"
    project_root = task_dir.parent.parent
    slug = str(project_root.resolve()).strip("/").replace("/", "-")
    persistent = home / "projects" / slug
    persistent.mkdir(parents=True, exist_ok=True)

    cap = pma.MAX_PREVENTION_RULES
    # Seed with exactly cap entries, each with unique text and an added_at
    seed_rules = []
    for i in range(cap):
        seed_rules.append({
            "rule_id": f"test-{i:012x}",
            "rule": f"seeded rule number {i}",
            "category": "test",
            "template": "advisory",
            "enforcement": "prompt-constraint",
            "rationale": "",
            "source_finding": "",
            "executor": "all",
            "added_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
        })

    rules_path = persistent / "prevention-rules.json"
    rules_path.write_text(json.dumps({"rules": seed_rules, "updated_at": "2026-01-28T00:00:00Z"}))

    analysis = {
        "prevention_rules": [
            {
                "rule": "Brand new rule that exceeds cap",
                "category": "security",
                "template": "advisory",
                "enforcement": "prompt-constraint",
                "rationale": "testing size cap",
                "source_finding": "f-cap",
                "executor": "all",
            }
        ]
    }

    apply_analysis(task_dir, analysis)

    updated = json.loads(rules_path.read_text())
    updated_rules = updated.get("rules", [])
    assert len(updated_rules) == cap, (
        f"Expected exactly {cap} rules after size cap trim, got {len(updated_rules)}"
    )


def test_apply_analysis_size_cap_emits_trim_event(tmp_path: Path, monkeypatch):
    """AC 17: when size cap trims entries, prevention_rules_trimmed events are emitted.

    Each trimmed entry must yield a log_event call with event_type
    'prevention_rules_trimmed' and payload fields: rule_id, category,
    added_at, source_task.
    """
    import postmortem_analysis as pma  # noqa: PLC0415

    task_dir = _make_task_dir(tmp_path)
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "dynos_home"))

    home = tmp_path / "dynos_home"
    project_root = task_dir.parent.parent
    slug = str(project_root.resolve()).strip("/").replace("/", "-")
    persistent = home / "projects" / slug
    persistent.mkdir(parents=True, exist_ok=True)

    cap = pma.MAX_PREVENTION_RULES
    seed_rules = []
    for i in range(cap):
        seed_rules.append({
            "rule_id": f"test-{i:012x}",
            "rule": f"seeded rule number {i}",
            "category": "test",
            "template": "advisory",
            "enforcement": "prompt-constraint",
            "rationale": "",
            "source_finding": "",
            "executor": "all",
            "added_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
        })

    rules_path = persistent / "prevention-rules.json"
    rules_path.write_text(json.dumps({"rules": seed_rules, "updated_at": "2026-01-28T00:00:00Z"}))

    emitted_events: list[dict] = []

    original_log_event = pma.log_event

    def capturing_log_event(root, event_type, **kwargs):
        emitted_events.append({"event_type": event_type, **kwargs})

    monkeypatch.setattr(pma, "log_event", capturing_log_event)

    analysis = {
        "prevention_rules": [
            {
                "rule": "Another new rule to trigger trim",
                "category": "security",
                "template": "advisory",
                "enforcement": "prompt-constraint",
                "rationale": "testing trim event",
                "source_finding": "f-trim",
                "executor": "all",
            }
        ]
    }

    apply_analysis(task_dir, analysis)

    trim_events = [e for e in emitted_events if e["event_type"] == "prevention_rules_trimmed"]
    assert trim_events, (
        "Expected at least one 'prevention_rules_trimmed' event but none were emitted. "
        f"All events: {[e['event_type'] for e in emitted_events]}"
    )

    for event in trim_events:
        for field in ("rule_id", "category", "added_at", "source_task"):
            assert field in event, (
                f"prevention_rules_trimmed event missing field {field!r}: {event}"
            )
