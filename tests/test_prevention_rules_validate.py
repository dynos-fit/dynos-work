"""Validation gate for prevention rules registry and fixture files.

Covers ACs 14 and 15 from task-20260507-004.

AC 14: Exactly two test functions:
  - test_validate_rules_valid_fixture (unit, no integration marker)
  - test_validate_rules_live_registry (integration marker)

AC 15: Fixture files exist and have correct structure.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

from rules_engine import _validate_rule_entry  # noqa: E402

_DISHONEST_ENFORCEMENT = {"ci-gate", "static-check", "runtime-guard", "lint", "test"}
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def test_validate_rules_valid_fixture():
    """AC 14 + AC 15: valid fixture has 3+ entries, 2+ templates, all pass _validate_rule_entry.

    This is a pure unit test — no integration marker.
    Also asserts zero entries have template=='advisory' AND enforcement in
    the dishonest set.
    """
    fixture_path = _FIXTURES_DIR / "prevention-rules-valid.json"
    assert fixture_path.exists(), (
        f"Fixture file missing: {fixture_path}. AC 15 requires it to exist."
    )

    data = json.loads(fixture_path.read_text())
    assert isinstance(data, dict), "Fixture must be a JSON object"
    rules = data.get("rules", [])
    assert len(rules) >= 3, (
        f"AC 15 requires at least 3 entries in valid fixture, got {len(rules)}"
    )

    templates_seen = set()
    for entry in rules:
        err = _validate_rule_entry(entry)
        assert err is None, (
            f"Valid fixture entry failed _validate_rule_entry: {err!r}\n"
            f"Entry: {entry}"
        )
        templates_seen.add(entry.get("template"))

    assert len(templates_seen) >= 2, (
        f"AC 15 requires at least 2 distinct templates, got {templates_seen!r}"
    )

    # Assert no dishonest enforcement labels (AC 14 invariant)
    dishonest = [
        e for e in rules
        if e.get("template") == "advisory"
        and e.get("enforcement") in _DISHONEST_ENFORCEMENT
    ]
    assert dishonest == [], (
        f"Valid fixture has {len(dishonest)} entries with template='advisory' "
        f"and dishonest enforcement label: {dishonest}"
    )


@pytest.mark.integration
def test_validate_rules_live_registry():
    """AC 14: validates the live prevention-rules.json registry.

    Skipped if the file does not exist. Fails descriptively on any invalid entry.
    Also asserts zero entries have dishonest enforcement labels (AC 9).
    """
    from lib_core import _persistent_project_dir  # noqa: PLC0415

    live_path = _persistent_project_dir(Path.cwd()) / "prevention-rules.json"

    if not live_path.exists():
        pytest.skip(f"Live registry not found at {live_path}")

    data = json.loads(live_path.read_text())
    rules = data.get("rules", [])

    errors: list[str] = []
    for i, entry in enumerate(rules):
        err = _validate_rule_entry(entry)
        if err:
            rule_id = entry.get("rule_id", "<missing>")
            errors.append(f"Entry[{i}] rule_id={rule_id!r}: {err}")

    assert not errors, (
        f"Live registry has {len(errors)} invalid entries:\n" + "\n".join(errors)
    )

    dishonest = [
        (i, e) for i, e in enumerate(rules)
        if e.get("template") == "advisory"
        and e.get("enforcement") in _DISHONEST_ENFORCEMENT
    ]
    assert dishonest == [], (
        f"Live registry has {len(dishonest)} entries with template='advisory' "
        f"and dishonest enforcement labels:\n"
        + "\n".join(
            f"  Entry[{i}] rule_id={e.get('rule_id', '<missing>')!r} "
            f"enforcement={e.get('enforcement')!r}"
            for i, e in dishonest
        )
    )
