"""Category-trend prevention rules written by the improvement cycle must pass
the rules validator (they used to have no rule_id, enforcement or params)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "memory"))

from lib_core import _persistent_project_dir  # noqa: E402
from postmortem_improve import apply_improvement  # noqa: E402
from rules_engine import _validate_rule_entry  # noqa: E402


def test_category_rule_is_valid_and_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.setenv("DYNOS_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    (root / ".dynos").mkdir(parents=True)
    rules_path = _persistent_project_dir(root) / "prevention-rules.json"
    rules_path.parent.mkdir(parents=True)
    rules_path.write_text(json.dumps({"rules": [{"rule_id": "sec-1", "category": "sec", "template": "advisory"}], "version": 3}))

    res = apply_improvement(root, {"id": "p2", "action": "add_prevention_rule", "category": "cmd", "total_occurrences": 12})
    assert res["applied"]
    data = json.loads(rules_path.read_text())
    assert data["version"] == 3
    assert [r["category"] for r in data["rules"]] == ["sec", "cmd"]
    new = data["rules"][1]
    assert _validate_rule_entry(new) is None
    assert new["rule_id"].startswith("cmd-")
    assert new["enforcement"] == "review-checklist"

    again = apply_improvement(root, {"id": "p3", "action": "add_prevention_rule", "category": "cmd", "total_occurrences": 13})
    assert not again["applied"]
    assert len(json.loads(rules_path.read_text())["rules"]) == 2
