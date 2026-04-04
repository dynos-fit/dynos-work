#!/usr/bin/env python3
"""Generate structured learned agent or skill markdown from retrospectives."""

from __future__ import annotations
import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
from collections import Counter
from pathlib import Path

from dynoslib import collect_retrospectives, now_iso, register_learned_agent


def build_body(root: Path, role: str, task_type: str, source_tasks: list[str]) -> str:
    findings = Counter()
    repairs = Counter()
    retrospectives = collect_retrospectives(root)
    for retrospective in retrospectives:
        if retrospective.get("task_type") != task_type:
            continue
        if source_tasks and retrospective.get("task_id") not in source_tasks:
            continue
        for category, count in retrospective.get("findings_by_category", {}).items():
            if isinstance(count, int):
                findings[str(category)] += count
        for executor, count in retrospective.get("executor_repair_frequency", {}).items():
            if isinstance(count, int):
                repairs[str(executor)] += count
    top_findings = [name for name, _ in findings.most_common(4)]
    top_repairs = [name for name, _ in repairs.most_common(4)]
    lines = [
        "## Operating Focus",
        f"- Optimize for {task_type} tasks with disciplined, low-rework execution.",
        f"- Prioritize the failure patterns most associated with `{role}` in this repo.",
        "",
        "## Prevention Rules",
    ]
    if top_findings:
        for category in top_findings:
            lines.append(f"- Prevent recurring `{category}` issues before writing code or approving output.")
    else:
        lines.append("- Prevent regressions by validating changed files, tests, and acceptance coverage before completion.")
    lines.extend(["", "## Repair Bias"])
    if top_repairs:
        for executor in top_repairs:
            lines.append(f"- When work touches patterns historically repaired by `{executor}`, add an explicit verification step.")
    else:
        lines.append("- Prefer smaller, verifiable steps and explicit evidence over speculative broad changes.")
    lines.extend(
        [
            "",
            "## Constraints",
            "- Never bypass deterministic validators or human approval gates.",
            "- Prefer repo-local evidence over recalled patterns when they conflict.",
            "- Minimize rework by checking ownership, tests, and audit-sensitive areas before concluding work.",
        ]
    )
    return "\n".join(lines) + "\n"


def cmd_generate(args: argparse.Namespace) -> int:
    root = Path(args.root).resolve()
    output = Path(args.output_path)
    if not output.is_absolute():
        output = root / output
    output.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = [
        "---",
        f"name: {args.agent_name}",
        f'description: "Learned {args.item_kind} for {args.role} on {args.task_type} tasks."',
        "source: learned",
        f"generated_from: {args.generated_from}",
        f"generated_at: {now_iso()}",
        "---",
        "",
    ]
    body = build_body(root, args.role, args.task_type, args.source_task)
    output.write_text("\n".join(frontmatter) + body)
    register_learned_agent(
        root,
        agent_name=args.agent_name,
        role=args.role,
        task_type=args.task_type,
        path=str(output.relative_to(root)),
        generated_from=args.generated_from,
        item_kind=args.item_kind,
    )
    print(str(output))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("agent_name")
    parser.add_argument("role")
    parser.add_argument("task_type")
    parser.add_argument("output_path")
    parser.add_argument("generated_from")
    parser.add_argument("--item-kind", choices=["agent", "skill"], default="agent")
    parser.add_argument("--source-task", action="append", default=[])
    parser.add_argument("--root", default=".")
    parser.set_defaults(func=cmd_generate)
    return parser


if __name__ == "__main__":
    from dyno_cli_base import cli_main
    raise SystemExit(cli_main(build_parser))
