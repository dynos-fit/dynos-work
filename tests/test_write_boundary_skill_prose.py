from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_plan_skill_uses_execution_graph_wrapper() -> None:
    text = _read("skills/plan/SKILL.md")
    assert "write-execution-graph" in text
    assert "Do not hand-write `.dynos/task-{id}/execution-graph.json`" in text


def test_start_skill_uses_execution_graph_wrapper() -> None:
    text = _read("skills/start/SKILL.md")
    assert "write-execution-graph" in text
    assert "write-classification" in text
    assert "Write the returned classification object to `manifest.json`." not in text


def test_audit_skill_uses_repair_log_wrapper() -> None:
    text = _read("skills/audit/SKILL.md")
    assert "write-repair-log" in text
    assert "Do not hand-write `.dynos/task-{id}/repair-log.json`" in text


def test_planning_agent_template_forbids_direct_graph_write() -> None:
    text = _read("cli/assets/templates/base/agents/planning.md")
    assert "write-execution-graph" in text
    assert "write-classification" in text
    assert "Do not hand-write `.dynos/task-{id}/execution-graph.json`" in text
    assert "manifest.json` under the `classification` key" not in text


def test_repair_coordinator_template_forbids_direct_repair_log_write() -> None:
    text = _read("cli/assets/templates/base/agents/repair-coordinator.md")
    assert "write-repair-log" in text
    assert "Do not hand-write `.dynos/task-{id}/repair-log.json`" in text


def test_live_planning_agent_forbids_direct_graph_write() -> None:
    text = _read("agents/planning.md")
    assert "write-execution-graph" in text
    assert "write-classification" in text
    assert "Do not hand-write `.dynos/task-{id}/execution-graph.json`" in text
    assert "manifest.json` under the `classification` key" not in text


def test_live_repair_coordinator_forbids_direct_repair_log_write() -> None:
    text = _read("agents/repair-coordinator.md")
    assert "write-repair-log" in text
    assert "Do not hand-write `.dynos/task-{id}/repair-log.json`" in text
