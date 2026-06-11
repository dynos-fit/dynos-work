"""Prose ↔ policy drift contract (§3 of docs/permissions-on-design.md).

Every command the pipeline skills prescribe must be allowed by the write
policy for the actor that runs it. This test extracts fenced command blocks
from the skill/agent markdown, runs them through the PRODUCTION Bash
destination extractor, and evaluates every detected write through the
PRODUCTION decide_write — so any future prose change that prescribes a
policy-denied action fails CI instead of failing a live run.

It would have caught every P0 in the permissions-on incident: /tmp staging,
`2>/dev/null` denials, `rm active-segment-role`, and the orchestrator
audit-report write.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hooks"))

from pre_tool_use import _extract_bash_destinations  # noqa: E402
from write_policy import WriteAttempt, decide_write  # noqa: E402

# The skills whose prose drives the start → execute → audit pipeline. The
# orchestrator (main session) runs every command block in these files.
PIPELINE_SKILLS = [
    "skills/start/SKILL.md",
    "skills/execute/SKILL.md",
    "skills/audit/SKILL.md",
    "skills/plan/SKILL.md",
    "skills/investigate/SKILL.md",
]

AGENT_FILES = sorted((ROOT / "agents").glob("*.md"))

_FENCE_RE = re.compile(r"```(?:bash|text|sh)\n(.*?)```", re.DOTALL)

# Template placeholders → concrete values so paths resolve.
_SUBSTITUTIONS = [
    ("{id}", "20260611-001"),
    ("{task-id}", "task-20260611-001"),
    ("{task_id}", "task-20260611-001"),
    ("{seg-id}", "seg-1"),
    ("{segment-id}", "seg-1"),
    ("$INVESTIGATION_DIR", ".dynos/investigations/20260611-000000"),
    ('"$AUDIT_CONTEXT_PATH"', ".dynos/task-20260611-001/audit-context.md"),
    ("${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}", "/fake-plugin-root"),
    ("${PLUGIN_ROOT}", "/fake-plugin-root"),
    ("${CLAUDE_PLUGIN_ROOT}", "/fake-plugin-root"),
    ("${PLUGIN_HOOKS}", "/fake-plugin-root/hooks"),
    ('"$DYNOS"', "/fake-plugin-root/bin/dynos"),
    ("$(pwd)", "."),
    ("$(date +%Y%m%d-%H%M%S)", "20260611-000000"),
]


def _command_blocks(markdown: str) -> list[str]:
    return [m.group(1) for m in _FENCE_RE.finditer(markdown)]


def _substituted(block: str) -> str:
    for old, new in _SUBSTITUTIONS:
        block = block.replace(old, new)
    return block


def _looks_like_commands(block: str) -> bool:
    """Skip fenced blocks that are output samples / log lines, not commands."""
    first = next((ln.strip() for ln in block.splitlines() if ln.strip()), "")
    if not first:
        return False
    if first.startswith(("{", "#", ">", "-", "|", "[", "Foundry", "dynos-work:")):
        return False
    return True


@pytest.fixture()
def task_env(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path
    task_dir = root / ".dynos" / "task-20260611-001"
    task_dir.mkdir(parents=True)
    return root, task_dir


@pytest.mark.parametrize("skill", PIPELINE_SKILLS)
def test_every_prescribed_write_is_policy_allowed_for_orchestrator(
    skill: str, task_env: tuple[Path, Path]
) -> None:
    root, task_dir = task_env
    markdown = (ROOT / skill).read_text(encoding="utf-8")
    violations: list[str] = []
    for block in _command_blocks(markdown):
        if not _looks_like_commands(block):
            continue
        command = _substituted(block)
        for dest in _extract_bash_destinations(command):
            path = Path(dest)
            if not path.is_absolute():
                path = root / dest
            decision = decide_write(
                WriteAttempt(
                    role="orchestrator",
                    task_dir=task_dir,
                    path=path,
                    operation="modify",
                    source="agent",
                )
            )
            if not decision.allowed:
                violations.append(
                    f"{skill}: block prescribes write to {dest!r} which the "
                    f"policy DENIES for the orchestrator: {decision.reason}\n"
                    f"--- block ---\n{block.strip()[:400]}"
                )
    assert violations == [], "\n\n".join(violations)


@pytest.mark.parametrize("skill", PIPELINE_SKILLS)
def test_no_forbidden_tokens_in_pipeline_skill_commands(skill: str) -> None:
    markdown = (ROOT / skill).read_text(encoding="utf-8")
    violations: list[str] = []
    for block in _command_blocks(markdown):
        if "/tmp/" in block:
            violations.append(f"{skill}: command block stages a /tmp path:\n{block.strip()[:200]}")
        if "PYTHONPATH=" in block:
            violations.append(
                f"{skill}: command block sets PYTHONPATH — use the dynos "
                f"funnel instead:\n{block.strip()[:200]}"
            )
        if re.search(r"\brm\b[^\n]*active-segment-role", block):
            violations.append(
                f"{skill}: command block deletes active-segment-role — use "
                f"`ctl clear-role`:\n{block.strip()[:200]}"
            )
        if re.search(r"python3 hooks/", block):
            violations.append(
                f"{skill}: repo-relative hook invocation breaks installed "
                f"plugins — use the dynos funnel:\n{block.strip()[:200]}"
            )
    assert violations == [], "\n\n".join(violations)


def test_no_tmp_staging_anywhere_in_skills_or_agents() -> None:
    """/tmp staging is forbidden across ALL skills and agents — the policy
    denies it for every non-executor actor, so prescribing it is drift."""
    offenders: list[str] = []
    for md in [*(ROOT / "skills").rglob("SKILL.md"), *AGENT_FILES]:
        text = md.read_text(encoding="utf-8")
        for block in _command_blocks(text):
            if "/tmp/" in block:
                offenders.append(str(md.relative_to(ROOT)))
                break
        else:
            # also catch inline-code prescriptions like `--from /tmp/x.json`
            if re.search(r"`[^`\n]*/tmp/[^`\n]*`", text):
                offenders.append(str(md.relative_to(ROOT)))
    assert offenders == [], f"/tmp staging prescribed in: {sorted(set(offenders))}"


def test_agents_without_write_capability_are_not_told_to_write_files() -> None:
    """An agent whose frontmatter lacks Write/Edit/Bash cannot materialize a
    file; its prose must route output through its final message instead."""
    violations: list[str] = []
    for md in AGENT_FILES:
        text = md.read_text(encoding="utf-8")
        m = re.search(r"^tools:\s*\[([^\]]*)\]", text, re.MULTILINE)
        if not m:
            continue
        tools = {t.strip() for t in m.group(1).split(",")}
        if tools & {"Write", "Edit", "Bash", "MultiEdit"}:
            continue
        # Read-only agent: forbid imperative file-write instructions.
        for pattern in (
            r"[Ww]rite\s+(?:its|your|the)\s+[^.\n]{0,60}\s+to\s+`/",
            r"[Ww]rite\s+[^.\n]{0,40}\s+to\s+`/tmp",
            r"persists?\s+[^.\n]{0,40}\s+via\s+`python3\s",
        ):
            if re.search(pattern, text):
                violations.append(f"{md.name}: read-only agent instructed to write/run: {pattern}")
    assert violations == [], "\n".join(violations)


def test_skill_prescribed_ctl_subcommands_exist() -> None:
    """Every `dynos ctl <subcommand>` the pipeline skills reference must be a
    registered ctl parser — catches prose referencing renamed commands."""
    import subprocess

    help_out = subprocess.run(
        ["python3", str(ROOT / "hooks" / "ctl.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    known = set(re.findall(r"[a-z][a-z0-9-]+", help_out))
    missing: list[str] = []
    for skill in PIPELINE_SKILLS:
        text = (ROOT / skill).read_text(encoding="utf-8")
        for cmd in re.findall(r'"\$DYNOS" ctl ([a-z][a-z0-9-]+)', text):
            if cmd not in known:
                missing.append(f"{skill}: ctl subcommand {cmd!r} not registered")
    assert missing == [], "\n".join(sorted(set(missing)))
