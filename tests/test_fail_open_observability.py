"""TDD-first tests for AC13 + AC14 + AC15 (task-20260419-009).

Three new observability emissions guard the silent fail-open branches:

  * AC13 — ``deferred_findings_check_unavailable`` emitted from
    ``hooks/lib_core.transition_task`` when importing
    ``check_deferred_findings`` raises ``ImportError`` before the
    ``_check_deferred = None`` fail-open assignment.
  * AC14 — ``deferred_findings_check_errored`` emitted from
    ``hooks/lib_core.transition_task`` when ``_check_deferred(...)``
    itself raises any exception, before ``_expired = []`` fail-open.
  * AC15 — ``hooks/check_deferred_findings._load_registry`` returns
    ``None`` only for "missing file"; a module-level
    ``_REGISTRY_PARSE_ERROR`` sentinel for malformed-JSON / OSError /
    non-dict shape.

Invariants (D3): a broken ``log_event`` MUST NOT block the fail-open
path. Each emission site is tested under monkeypatched ``log_event ->
raise``; the production path must still complete successfully.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))


def _setup_done_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    slug: str = "FOD",
) -> Path:
    """Build a task at CHECKPOINT_AUDIT ready for DONE — but WITHOUT
    actually wiring all DONE receipts. We only need the transition to
    reach the deferred-findings branch.

    Mocks ``router._load_auditor_registry`` to return empty lists so
    ``require_receipts_for_done``'s registry-eligible cross-check is
    vacuous and the transition actually reaches the AC13/AC14
    deferred-findings branch under test (otherwise it would refuse at
    the audit-routing cross-check with "audit-routing missing
    registry-eligible auditor: <name>" BEFORE the fail-open branch runs).
    """
    from lib_receipts import (  # noqa: PLC0415
        receipt_retrospective,
        receipt_rules_check_passed,
        receipt_audit_routing,
        receipt_postmortem_skipped,
    )

    # Prevent the registry-eligible auditor cross-check from short-circuiting
    # the DONE transition before it reaches the deferred-findings branch.
    import router  # noqa: PLC0415
    monkeypatch.setattr(router, "_load_auditor_registry", lambda root: {
        "always": [], "fast_track": [], "domain_conditional": {},
    })

    project = tmp_path / "project"
    td = project / ".dynos" / f"task-20260419-{slug}"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(
        json.dumps({
            "task_id": td.name,
            "stage": "CHECKPOINT_AUDIT",
            "classification": {"risk_level": "low"},
        })
    )
    (td / "task-retrospective.json").write_text(
        json.dumps({
            "task_id": td.name,
            "quality_score": 0.9,
            "cost_score": 0.8,
            "efficiency_score": 0.8,
        })
    )
    audit_dir = td / "audit-reports"
    audit_dir.mkdir()
    (audit_dir / "report.json").write_text(json.dumps({"findings": []}))

    receipt_retrospective(td)
    import rules_engine  # noqa: PLC0415
    _orig = rules_engine.run_checks
    rules_engine.run_checks = lambda root, mode: []
    try:
        receipt_rules_check_passed(td, "all")
    finally:
        rules_engine.run_checks = _orig
    receipt_audit_routing(td, [])
    receipt_postmortem_skipped(td, "no-findings", "a" * 64, subsumed_by=[])
    return td


def _read_events(td: Path) -> list[dict]:
    ev = td / "events.jsonl"
    if not ev.exists():
        return []
    return [json.loads(line) for line in ev.read_text().splitlines() if line.strip()]


# --- AC13: ImportError on check_deferred_findings emits named event ------

def test_deferred_findings_check_unavailable_emitted_on_import_error(
    tmp_path, monkeypatch,
):
    """Force the import to fail and assert the named event is emitted."""
    # Remove the real module from sys.modules so it re-imports, then
    # inject a shim that raises ImportError at import time.
    monkeypatch.delitem(sys.modules, "check_deferred_findings", raising=False)

    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "check_deferred_findings":
            raise ImportError("simulated missing module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    from lib_core import transition_task  # noqa: PLC0415
    from lib_receipts import RECEIPT_CONTRACT_VERSION  # noqa: F401, PLC0415

    td = _setup_done_ready(tmp_path, monkeypatch, slug="UNAVAIL")
    # The DONE transition should still succeed (fail-open).
    transition_task(td, "DONE")

    events = _read_events(td)
    names = [e.get("event") for e in events]
    assert "deferred_findings_check_unavailable" in names, (
        f"expected deferred_findings_check_unavailable event; got {names!r}"
    )
    # And carry a task= attribution.
    ev = [e for e in events if e.get("event") == "deferred_findings_check_unavailable"][0]
    assert ev.get("task") == td.name, (
        f"event must carry task=<task_dir.name>; got {ev!r}"
    )


def test_deferred_findings_unavailable_fail_open_if_log_event_raises(
    tmp_path, monkeypatch,
):
    """D3: if log_event raises while trying to emit the
    deferred_findings_check_unavailable event, the DONE transition must
    still succeed.
    """
    from lib_core import transition_task  # noqa: PLC0415
    # IMPORTANT: build the fixture BEFORE installing the log_event-explodes
    # patch — the fixture writers legitimately emit events and must not be
    # torched by the D3 probe (which is scoped to the DONE transition).
    td = _setup_done_ready(tmp_path, monkeypatch, slug="UNA-FOP")

    monkeypatch.delitem(sys.modules, "check_deferred_findings", raising=False)

    import builtins
    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name == "check_deferred_findings":
            raise ImportError("simulated missing module")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    import lib_log
    def _explode(*a, **kw):
        raise Exception("forced log_event failure")
    monkeypatch.setattr(lib_log, "log_event", _explode)

    # Must not raise.
    transition_task(td, "DONE")

    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "DONE", (
        "fail-open path must still complete the transition when log_event explodes"
    )


# --- AC14: _check_deferred raises => deferred_findings_check_errored ----

def test_deferred_findings_check_errored_emitted_on_exception(
    tmp_path, monkeypatch,
):
    """Force ``check_deferred_findings(root, changed_files)`` to raise."""
    from lib_core import transition_task  # noqa: PLC0415

    import check_deferred_findings as cdf

    def _boom(root, changed_files):
        raise RuntimeError("simulated registry corruption")

    monkeypatch.setattr(cdf, "check_deferred_findings", _boom)

    td = _setup_done_ready(tmp_path, monkeypatch, slug="ERRORED")
    transition_task(td, "DONE")

    events = _read_events(td)
    names = [e.get("event") for e in events]
    assert "deferred_findings_check_errored" in names, (
        f"expected deferred_findings_check_errored event; got {names!r}"
    )
    ev = [e for e in events if e.get("event") == "deferred_findings_check_errored"][0]
    assert ev.get("task") == td.name
    # Include the error message for forensic context.
    err = ev.get("error", "")
    assert "simulated registry corruption" in err or isinstance(err, str), (
        f"event must carry a string error; got {ev!r}"
    )


def test_deferred_findings_errored_fail_open_if_log_event_raises(
    tmp_path, monkeypatch,
):
    """D3: log_event raising must not block the DONE fail-open."""
    from lib_core import transition_task  # noqa: PLC0415
    import check_deferred_findings as cdf

    # IMPORTANT: build the fixture BEFORE installing the log_event-explodes
    # patch so fixture writers can legitimately emit events.
    td = _setup_done_ready(tmp_path, monkeypatch, slug="ERR-FOP")

    def _boom(root, changed_files):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(cdf, "check_deferred_findings", _boom)

    import lib_log
    def _explode(*a, **kw):
        raise Exception("forced log_event failure")
    monkeypatch.setattr(lib_log, "log_event", _explode)

    transition_task(td, "DONE")
    manifest = json.loads((td / "manifest.json").read_text())
    assert manifest["stage"] == "DONE"


# --- AC15: _load_registry sentinel distinguishes missing vs parse-error --

def test_load_registry_returns_none_only_for_missing_file(tmp_path):
    """When the registry path does not exist, _load_registry returns None.
    This is the ONLY case None is a valid return under AC15.
    """
    import check_deferred_findings as cdf

    # Clean root — deferred-findings.json absent.
    result = cdf._load_registry(tmp_path)
    assert result is None, (
        f"_load_registry must return None for missing file; got {result!r}"
    )


def test_load_registry_returns_sentinel_on_malformed_json(tmp_path):
    """AC15: malformed JSON returns the module-level _REGISTRY_PARSE_ERROR
    sentinel, NOT None.
    """
    import check_deferred_findings as cdf

    assert hasattr(cdf, "_REGISTRY_PARSE_ERROR"), (
        "AC15: hooks/check_deferred_findings.py must define a module-level "
        "sentinel _REGISTRY_PARSE_ERROR"
    )

    # Write malformed JSON.
    reg = tmp_path / ".dynos" / "deferred-findings.json"
    reg.parent.mkdir(parents=True)
    reg.write_text("this is not json {{{")

    result = cdf._load_registry(tmp_path)
    assert result is cdf._REGISTRY_PARSE_ERROR, (
        f"_load_registry must return _REGISTRY_PARSE_ERROR sentinel on "
        f"malformed JSON; got {result!r}"
    )


def test_load_registry_returns_sentinel_on_non_dict_shape(tmp_path):
    """AC15: well-formed JSON that is not a dict also returns the sentinel."""
    import check_deferred_findings as cdf

    reg = tmp_path / ".dynos" / "deferred-findings.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps(["a", "b", "c"]))  # list, not dict

    result = cdf._load_registry(tmp_path)
    assert result is cdf._REGISTRY_PARSE_ERROR, (
        f"_load_registry must return _REGISTRY_PARSE_ERROR for non-dict "
        f"shape; got {result!r}"
    )


def test_load_registry_happy_path_still_returns_dict(tmp_path):
    """Regression: a well-formed dict must still return the dict."""
    import check_deferred_findings as cdf

    reg = tmp_path / ".dynos" / "deferred-findings.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps({"findings": []}))

    result = cdf._load_registry(tmp_path)
    assert isinstance(result, dict) and result.get("findings") == []
