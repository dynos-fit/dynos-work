"""Tests for task-20260423-001 AC22: risk_level upward override.

Covers ``_normalize_classification_payload`` in ``hooks/ctl.py``:

  (a) planner ``low`` + spec text "delete user account" → upgraded to
      ``high`` with triggering signal ``keyword_scan``.
  (b) planner ``low`` + 12 ``files_expected`` → upgraded to ``high`` with
      triggering signal ``file_domain``.
  (c) planner ``medium`` + no triggering signals → left at ``medium``.
  (d) planner ``high`` + no triggering signals → left at ``high`` (no
      downgrade).
  (e) the upgrade emits exactly one ``risk_level_upgrade_blocked`` event
      per normalization call.
  (f) running the normalizer twice with the same inputs yields the same
      output + one additional event per call (idempotent on payload; the
      event emission is the side-effect).

All tests are TDD-first: if the normalizer does not yet compute the
``observed_floor`` / override logic, every test under this module will
fail — that is the expected shape of a TDD-first regression net.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))


try:
    ctl = importlib.import_module("ctl")
except Exception as exc:  # pragma: no cover - defensive import guard
    pytest.skip(
        f"hooks/ctl.py could not be imported for risk-upgrade tests: {exc}",
        allow_module_level=True,
    )

if not hasattr(ctl, "_normalize_classification_payload"):
    pytest.skip(
        "ctl._normalize_classification_payload not present (TDD-first)",
        allow_module_level=True,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _build_task_dir(
    tmp_path: Path,
    *,
    raw_input: str = "",
    spec: str = "",
    files_expected: list[str] | None = None,
) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    (root / ".dynos").mkdir()
    task_dir = root / ".dynos" / "task-20260423-999"
    task_dir.mkdir()
    (task_dir / "manifest.json").write_text(
        json.dumps({"task_id": task_dir.name}, indent=2)
    )
    (task_dir / "raw-input.md").write_text(raw_input)
    (task_dir / "spec.md").write_text(spec)
    if files_expected is not None:
        graph = {
            "task_id": task_dir.name,
            "segments": [
                {
                    "id": "seg-0",
                    "executor": "backend-executor",
                    "description": "d",
                    "files_expected": list(files_expected),
                    "depends_on": [],
                    "parallelizable": False,
                    "criteria_ids": [1],
                }
            ],
        }
        (task_dir / "execution-graph.json").write_text(json.dumps(graph))
    return task_dir


def _read_events(task_dir: Path) -> list[dict]:
    events_path = task_dir / "events.jsonl"
    if not events_path.exists():
        return []
    out: list[dict] = []
    for line in events_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _upgrade_events(task_dir: Path) -> list[dict]:
    return [r for r in _read_events(task_dir) if r.get("event") == "risk_level_upgrade_blocked"]


# ---------------------------------------------------------------------------
# (a) planner low + "delete user account" in spec → high, signal=keyword_scan
# ---------------------------------------------------------------------------
def test_keyword_scan_upgrades_low_to_high(tmp_path: Path) -> None:
    task_dir = _build_task_dir(
        tmp_path,
        raw_input="Short raw input.",
        spec="We must delete user account records when requested.",
    )
    payload = {
        "type": "feature",
        "domains": ["backend"],
        "risk_level": "low",
    }

    out = ctl._normalize_classification_payload(task_dir, payload)

    assert out["risk_level"] == "high", (
        f"keyword 'delete' in spec must upgrade low → high; got "
        f"risk_level={out.get('risk_level')!r}"
    )

    events = _upgrade_events(task_dir)
    assert len(events) == 1, (
        f"expected exactly one risk_level_upgrade_blocked event; got {len(events)}"
    )
    signals = events[0].get("triggering_signals", [])
    assert "keyword_scan" in signals, (
        f"expected 'keyword_scan' in triggering_signals; got {signals!r}"
    )


# ---------------------------------------------------------------------------
# (b) planner low + 12 files_expected → high, signal=file_domain
# ---------------------------------------------------------------------------
def test_file_domain_heuristic_upgrades_with_many_files(tmp_path: Path) -> None:
    files = [f"src/mod_{i}.py" for i in range(12)]
    task_dir = _build_task_dir(
        tmp_path,
        raw_input="Neutral raw input.",
        spec="No triggering keywords here whatsoever.",
        files_expected=files,
    )
    payload = {
        "type": "feature",
        "domains": ["backend"],
        "risk_level": "low",
        # Also expose via payload in case the implementation reads payload
        # before falling back to execution-graph.json.
        "files_expected": files,
    }

    out = ctl._normalize_classification_payload(task_dir, payload)

    assert out["risk_level"] == "high", (
        f"12 files_expected must upgrade low → high via file_domain; got "
        f"risk_level={out.get('risk_level')!r}"
    )
    events = _upgrade_events(task_dir)
    assert len(events) == 1
    assert "file_domain" in events[0].get("triggering_signals", []), (
        f"expected 'file_domain' in triggering_signals; got "
        f"{events[0].get('triggering_signals')!r}"
    )


# ---------------------------------------------------------------------------
# (c) planner medium + no triggering signals → stays medium
# ---------------------------------------------------------------------------
def test_medium_without_signals_is_not_changed(tmp_path: Path) -> None:
    task_dir = _build_task_dir(
        tmp_path,
        raw_input="Small config tweak.",
        spec="Neutral short spec with no flagged words.",
        files_expected=["src/one.py"],
    )
    payload = {
        "type": "feature",
        "domains": ["backend"],
        "risk_level": "medium",
    }

    out = ctl._normalize_classification_payload(task_dir, payload)

    assert out["risk_level"] == "medium", (
        f"medium with no signals must stay medium; got "
        f"risk_level={out.get('risk_level')!r}"
    )

    events = _upgrade_events(task_dir)
    assert events == [], (
        f"no upgrade event expected when nothing triggers; got {events!r}"
    )


# ---------------------------------------------------------------------------
# (d) planner high + no triggering signals → stays high (no downgrade)
# ---------------------------------------------------------------------------
def test_high_is_not_downgraded_when_no_signals(tmp_path: Path) -> None:
    task_dir = _build_task_dir(
        tmp_path,
        raw_input="Planner-judged high risk despite small footprint.",
        spec="No flagged keywords.",
        files_expected=["src/one.py"],
    )
    payload = {
        "type": "feature",
        "domains": ["backend"],
        "risk_level": "high",
    }

    out = ctl._normalize_classification_payload(task_dir, payload)

    assert out["risk_level"] == "high", (
        "planner high must never be downgraded; the normalizer is upward-only"
    )
    events = _upgrade_events(task_dir)
    assert events == [], (
        f"no upgrade event expected when planner already at high; got {events!r}"
    )


# ---------------------------------------------------------------------------
# (e) A single triggering call emits exactly one risk_level_upgrade_blocked
#     event, even when multiple signals fire simultaneously. Spec AC9
#     demands the event is emitted once per normalization call, not once
#     per signal.
# ---------------------------------------------------------------------------
def test_multiple_signals_emit_single_event(tmp_path: Path) -> None:
    files = [f"src/mod_{i}.py" for i in range(12)]
    task_dir = _build_task_dir(
        tmp_path,
        raw_input="Includes delete operations.",
        spec="We will delete user account records and migrate the schema.",
        files_expected=files,
    )
    payload = {
        "type": "feature",
        "domains": ["backend"],
        "risk_level": "low",
        "files_expected": files,
    }

    ctl._normalize_classification_payload(task_dir, payload)

    events = _upgrade_events(task_dir)
    assert len(events) == 1, (
        f"exactly one risk_level_upgrade_blocked event must be emitted per "
        f"normalization call regardless of how many signals fire; got {len(events)}"
    )
    signals = events[0].get("triggering_signals", [])
    # Both signals must be recorded in the single event.
    assert "keyword_scan" in signals
    assert "file_domain" in signals


# ---------------------------------------------------------------------------
# (f) Idempotence on payload, one event per call
# ---------------------------------------------------------------------------
def test_idempotent_payload_but_one_event_per_call(tmp_path: Path) -> None:
    task_dir = _build_task_dir(
        tmp_path,
        raw_input="Must delete user account data.",
        spec="Please delete user account info on request.",
    )
    payload_1 = {"type": "feature", "domains": ["backend"], "risk_level": "low"}
    # Deep-copy via json so the implementation can't silently mutate ours.
    payload_2 = json.loads(json.dumps(payload_1))

    out_1 = ctl._normalize_classification_payload(task_dir, payload_1)
    out_2 = ctl._normalize_classification_payload(task_dir, payload_2)

    # Payload outputs are identical — the override is idempotent.
    assert out_1.get("risk_level") == out_2.get("risk_level") == "high", (
        f"normalizer must produce the same upgraded risk_level on repeat calls; "
        f"got {out_1.get('risk_level')!r} vs {out_2.get('risk_level')!r}"
    )

    # Each call emits exactly one event; side effects accumulate.
    events = _upgrade_events(task_dir)
    assert len(events) == 2, (
        "two normalize calls must produce two events (one per call); the "
        f"payload is idempotent but event emission is the side effect. Got "
        f"{len(events)} event(s)."
    )


# ---------------------------------------------------------------------------
# Regression guard: a planner re-asserting ``risk_level: low`` after the
# observed_floor has been seen must not launder the task back to low.
# Spec AC9: "The planner cannot suppress the override by re-asserting a
# lower risk_level in the payload."
# ---------------------------------------------------------------------------
def test_planner_cannot_launder_risk_by_reasserting_low(tmp_path: Path) -> None:
    task_dir = _build_task_dir(
        tmp_path,
        raw_input="Will delete user account.",
        spec="Delete user account data on request.",
    )

    # First call upgrades to high.
    out_1 = ctl._normalize_classification_payload(
        task_dir, {"type": "feature", "domains": ["backend"], "risk_level": "low"}
    )
    assert out_1["risk_level"] == "high"

    # Planner tries again with the same inputs — must still upgrade.
    out_2 = ctl._normalize_classification_payload(
        task_dir, {"type": "feature", "domains": ["backend"], "risk_level": "low"}
    )
    assert out_2["risk_level"] == "high", (
        "planner must not be able to launder high-signal task back to low by "
        "re-asserting a lower risk_level"
    )
