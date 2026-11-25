#!/usr/bin/env python3
"""Regression & parity tests for performance, C fast-path, calibration, custom
auditors/roles, and smart router gating fixes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"
MEMORY_DIR = ROOT / "memory"
FAST_BIN = HOOKS_DIR / "bin" / "dynos-fast"

for _p in (HOOKS_DIR, MEMORY_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def test_fire_task_completed_dispatches_in_external_repo_without_plugin_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import lib_core

    ext_repo = tmp_path / "external-app"
    task_dir = ext_repo / ".dynos" / "task-20261124-001"
    task_dir.mkdir(parents=True)
    monkeypatch.delenv("PLUGIN_HOOKS", raising=False)

    run_calls: list[dict] = []
    popen_calls: list[dict] = []

    def fake_run(cmd, **kwargs):
        run_calls.append({"cmd": cmd, "env": kwargs.get("env", {})})
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def fake_popen(cmd, **kwargs):
        popen_calls.append({"cmd": cmd, "env": kwargs.get("env", {})})
        return mock.MagicMock()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    lib_core._fire_task_completed(task_dir)

    assert len(run_calls) == 1
    assert len(popen_calls) == 1
    assert str(HOOKS_DIR / "lib_events.py") in run_calls[0]["cmd"][1]
    assert str(HOOKS_DIR / "eventbus.py") in popen_calls[0]["cmd"][1]
    assert popen_calls[0]["env"].get("PLUGIN_HOOKS") == str(HOOKS_DIR)
    drain_log = ext_repo / ".dynos" / "events" / "drain.log"
    assert "SKIPPED" not in drain_log.read_text(encoding="utf-8")


def test_shadow_calibration_canary_routes_existing_learned_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import router

    persistent = tmp_path / "persistent"
    learned_file = persistent / "learned-agents" / "executors" / "auto-backend-feature.md"
    learned_file.parent.mkdir(parents=True)
    learned_file.write_text("# Learned backend\n", encoding="utf-8")

    monkeypatch.setattr(router, "_persistent_project_dir", lambda _r: persistent)
    monkeypatch.setattr(router, "is_learning_enabled", lambda _r: True)
    monkeypatch.setattr(router, "log_event", lambda *a, **kw: None)
    monkeypatch.setattr(
        router,
        "_read_learned_registry",
        lambda _r: {
            "agents": [
                {
                    "agent_name": "auto-backend-feature",
                    "role": "backend-executor",
                    "task_type": "feature",
                    "path": str(learned_file),
                    "mode": "shadow",
                    "status": "active",
                    "benchmark_summary": {"sample_count": 0},
                },
                {
                    "agent_name": "auto-sec-feature",
                    "role": "security-auditor",
                    "task_type": "feature",
                    "path": str(learned_file),
                    "mode": "shadow",
                    "status": "active",
                    "benchmark_summary": {"sample_count": 0},
                },
            ]
        },
    )

    exec_route = router.resolve_route(tmp_path, "backend-executor", "feature")
    assert exec_route["mode"] == "learned"
    assert exec_route["source"] == "shadow_canary:auto-backend-feature"

    sec_route = router.resolve_route(tmp_path, "security-auditor", "feature")
    assert sec_route["mode"] == "generic"


def test_agent_generator_generates_custom_auditors_and_project_roles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_generator
    import lib_registry

    persistent = tmp_path / "persistent"
    monkeypatch.setattr(agent_generator, "_persistent_project_dir", lambda _r: persistent)
    monkeypatch.setattr(lib_registry, "learned_registry_path", lambda _r: persistent / "learned-agents" / "registry.json")
    monkeypatch.setattr(agent_generator, "log_event", lambda *a, **kw: None)

    cfg_dir = tmp_path / ".dynos" / "config"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "roles.json").write_text(
        json.dumps(
            {
                "roles": [
                    {"role": "billing-executor", "task_type": "feature", "description": "Handles billing webhooks"},
                    {"role": "compliance-auditor", "task_type": "feature", "domains": ["security"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    retros = [
        {
            "task_id": f"task-20261124-00{i}",
            "task_type": "feature",
            "model_used_by_agent": {"backend-executor": "sonnet", "api-contract-auditor": "sonnet"},
            "findings_by_auditor": {"api-contract-auditor": 1 if i == 1 else 0},
            "findings_by_category": {"comp": 1},
            "_path": str(persistent / "retrospectives" / f"task-20261124-00{i}.json"),
        }
        for i in range(1, 5)
    ]
    monkeypatch.setattr(agent_generator, "collect_retrospectives", lambda _r: retros)

    import argparse
    import io
    with mock.patch("sys.stdout", new_callable=io.StringIO) as buf:
        rc = agent_generator.cmd_auto(argparse.Namespace(root=str(tmp_path), min_tasks=3))
    assert rc == 0
    out = json.loads(buf.getvalue())
    gen_roles = {item["role"] for item in out["generated"]}
    assert "backend-executor" in gen_roles
    assert "api-contract-auditor" in gen_roles
    assert "billing-executor" in gen_roles
    assert "compliance-auditor" in gen_roles
    assert (persistent / "learned-agents" / "auditors" / "auto-audit-api-contract-feature.md").is_file()
    assert (persistent / "learned-agents" / "executors" / "billing-executor.md").is_file()
    assert (persistent / "learned-agents" / "auditors" / "compliance-auditor.md").is_file()


def test_dynamic_project_roles_accepted_everywhere(tmp_path: Path) -> None:
    import ctl
    import lib_core
    import pre_tool_use

    cfg_dir = tmp_path / ".dynos" / "config"
    task_dir = tmp_path / ".dynos" / "task-20261124-001"
    cfg_dir.mkdir(parents=True)
    task_dir.mkdir(parents=True)
    (cfg_dir / "roles.json").write_text(
        json.dumps({"roles": ["billing-executor", "compliance-auditor"]}),
        encoding="utf-8",
    )

    assert lib_core.role_for_subagent_type("billing-executor", project_root=tmp_path) == "billing-executor"
    assert lib_core.role_for_subagent_type("compliance-auditor", project_root=tmp_path) == "audit-compliance"
    assert lib_core.is_allowed_project_role("ctl", tmp_path) is False
    assert lib_core.is_allowed_project_role("system", tmp_path) is False

    assert ctl._validated_grant_role("stamp-role", "billing-executor", task_dir=task_dir) == "billing-executor"
    assert ctl._validated_grant_role("stamp-role", "audit-compliance", task_dir=task_dir) == "audit-compliance"
    assert ctl._validated_grant_role("stamp-role", "ctl", task_dir=task_dir) is None

    assert pre_tool_use._is_allowed_role("billing-executor", tmp_path) is True
    assert pre_tool_use._is_allowed_role("audit-compliance", tmp_path) is True
    assert pre_tool_use._is_allowed_role("ctl", tmp_path) is False


def test_non_blocking_findings_preserve_zero_finding_streak(tmp_path: Path) -> None:
    import lib_receipts
    import lib_validate

    task_dir = tmp_path / ".dynos" / "task-20261124-002"
    (task_dir / "audit-reports").mkdir(parents=True)
    (task_dir / "manifest.json").write_text(
        json.dumps({
            "task_id": "task-20261124-002",
            "stage": "CHECKPOINT_AUDIT",
            "classification": {"type": "feature", "domains": ["backend"], "risk_level": "medium"},
        }),
        encoding="utf-8",
    )
    (task_dir / "audit-reports" / "code-quality-auditor.json").write_text(
        json.dumps({
            "auditor_name": "code-quality-auditor",
            "findings": [
                {"id": "CQ-1", "severity": "info", "blocking": False, "description": "minor nit"},
            ],
        }),
        encoding="utf-8",
    )
    lib_receipts.receipt_audit_routing(
        task_dir,
        [{"name": "code-quality-auditor", "action": "spawn", "route_mode": "generic", "agent_path": None, "injected_agent_sha256": None}],
    )
    res = subprocess.run(
        [sys.executable, str(HOOKS_DIR / "ctl.py"), "run-audit-reflect", str(task_dir)],
        capture_output=True, text=True,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    retro = json.loads((task_dir / "task-retrospective.json").read_text(encoding="utf-8"))
    assert retro["findings_by_auditor"]["code-quality-auditor"] == 1
    assert retro["auditor_zero_finding_streaks"]["code-quality-auditor"] == 1


def test_router_fast_track_and_risk_gates_and_batch_inject(tmp_path: Path) -> None:
    import router

    fast_plan = router.build_audit_plan(
        tmp_path,
        "bugfix",
        ["backend"],
        fast_track=True,
        risk_level="medium",
        task_id="task-fast-001",
        diff_files=["hooks/ctl.py"],
    )
    by_name = {e["name"]: e for e in fast_plan["auditors"]}
    assert by_name["security-auditor"]["ensemble"] is False
    assert by_name["claude-md-auditor"]["action"] == "skip"

    med_plan = router.build_audit_plan(
        tmp_path,
        "feature",
        ["backend"],
        fast_track=False,
        risk_level="medium",
        task_id="task-med-001",
        diff_files=["src/service.py"],
    )
    med_by_name = {e["name"]: e for e in med_plan["auditors"]}
    assert med_by_name["docs-accuracy-auditor"]["action"] == "skip"
    assert med_by_name["dead-code-auditor"]["action"] == "skip"
    assert med_by_name["architecture-auditor"]["ensemble"] is False

    # audit-inject-batch returns sidecar digests + valid JSON instructions
    td = tmp_path / ".dynos" / "task-20261124-009"
    td.mkdir(parents=True)
    ap_path = td / "audit-plan.json"
    ap_path.write_text(json.dumps(med_plan), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(HOOKS_DIR / "router.py"),
            "audit-inject-batch",
            "--root", str(tmp_path),
            "--task-type", "feature",
            "--audit-plan", str(ap_path),
        ],
        input=json.dumps([{"auditor_name": "architecture-auditor", "model": None, "prompt": "Audit arch"}]),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    batch_out = json.loads(proc.stdout)
    assert len(batch_out) == 1
    assert batch_out[0]["sha256"]
    assert "never markdown" in batch_out[0]["prompt"].lower() or "no markdown" in batch_out[0]["prompt"].lower()


def test_native_fast_path_binary_contracts(tmp_path: Path) -> None:
    import shutil

    c_src = HOOKS_DIR / "native_fast.c"
    cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
    if not cc:
        pytest.skip("no C compiler available")
    fast_bin = tmp_path / "dynos-fast"
    comp = subprocess.run([cc, "-O3", "-o", str(fast_bin), str(c_src)], capture_output=True, text=True)
    assert comp.returncode == 0, comp.stderr

    non_dynos = tmp_path / "plain-repo"
    non_dynos.mkdir()

    # Non-dynos Read -> exit 0 fast-allow
    r_read = subprocess.run(
        [str(fast_bin), "--pre-tool-use"],
        input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "README.md"}, "cwd": str(non_dynos)}),
        capture_output=True, text=True,
    )
    assert r_read.returncode == 0

    # Write tool always delegates (exit 10)
    r_write = subprocess.run(
        [str(fast_bin), "--pre-tool-use"],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": "x.py"}, "cwd": str(non_dynos)}),
        capture_output=True, text=True,
    )
    assert r_write.returncode == 10

    # Active dynos task delegates Read -> exit 10
    dynos_repo = tmp_path / "dynos-repo"
    td = dynos_repo / ".dynos" / "task-20261124-001"
    td.mkdir(parents=True)
    (td / "manifest.json").write_text(json.dumps({"stage": "CHECKPOINT_AUDIT"}), encoding="utf-8")
    r_active = subprocess.run(
        [str(fast_bin), "--pre-tool-use"],
        input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "src/a.py"}, "cwd": str(dynos_repo)}),
        capture_output=True, text=True,
    )
    assert r_active.returncode == 10

    # Terminal dynos task fast-allows Read -> exit 0
    (td / "manifest.json").write_text(json.dumps({"stage": "DONE"}), encoding="utf-8")
    r_done = subprocess.run(
        [str(fast_bin), "--pre-tool-use"],
        input=json.dumps({"tool_name": "Read", "tool_input": {"file_path": "src/a.py"}, "cwd": str(dynos_repo)}),
        capture_output=True, text=True,
    )
    assert r_done.returncode == 0

    # SubagentStop missing transcript -> exit 0
    r_sub = subprocess.run(
        [str(fast_bin), "--subagent-tokens"],
        input=json.dumps({"agent_transcript_path": "", "cwd": str(dynos_repo)}),
        capture_output=True, text=True,
    )
    assert r_sub.returncode == 0
