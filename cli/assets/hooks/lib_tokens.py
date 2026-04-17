#!/usr/bin/env python3
"""Deterministic token usage tracking for dynos-work tasks.

CLI usage (called by start, execute, and audit skills after every event):

    # Record a subagent spawn
    python3 lib_tokens.py record \
        --task-dir .dynos/task-20260406-001 \
        --agent "backend-executor-seg1" \
        --model "opus" \
        --input-tokens 12000 \
        --output-tokens 4500 \
        --phase execution \
        --stage "EXECUTION" \
        --segment "seg-1"

    # Record a deterministic validation (no tokens, just event)
    python3 lib_tokens.py record \
        --task-dir .dynos/task-20260406-001 \
        --agent "validate_task_artifacts" \
        --model "none" \
        --input-tokens 0 \
        --output-tokens 0 \
        --phase planning \
        --stage "PLAN_AUDIT" \
        --type deterministic \
        --detail "Validated plan.md and execution-graph.json"

    # Print summary
    python3 lib_tokens.py summary --task-dir .dynos/task-20260406-001

File format (.dynos/task-{id}/token-usage.json):

    {
      "agents": {"agent-name": 16500},
      "by_agent": {
        "agent-name": {
          "input_tokens": 12000, "output_tokens": 4500,
          "tokens": 16500, "model": "opus"
        }
      },
      "by_model": {
        "opus": {"input_tokens": 12000, "output_tokens": 4500, "tokens": 16500}
      },
      "total": 16500,
      "total_input_tokens": 12000,
      "total_output_tokens": 4500,
      "events": [
        {
          "timestamp": "2026-04-06T10:30:00Z",
          "agent": "backend-executor-seg1",
          "model": "opus",
          "input_tokens": 12000,
          "output_tokens": 4500,
          "tokens": 16500,
          "phase": "execution",
          "stage": "EXECUTION",
          "type": "spawn",
          "segment": "seg-1",
          "detail": null
        }
      ]
    }

Event types:
  - "spawn"          : LLM subagent was spawned (default)
  - "deterministic"  : Deterministic Python validation/check (no LLM, 0 tokens)
  - "inline"         : Fast-track inline execution (no subagent spawn)
"""

from __future__ import annotations

import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib_core import load_json, write_json


EMPTY_USAGE: dict = {
    "agents": {},
    "by_agent": {},
    "by_model": {},
    "total": 0,
    "total_input_tokens": 0,
    "total_output_tokens": 0,
    "events": [],
}

VALID_PHASES = {"planning", "execution", "audit", "tdd", "repair", "completion"}
VALID_TYPES = {"spawn", "deterministic", "inline", "stage-transition", "gate"}

# Maps manifest stages to event phases (deterministic, no LLM dependency)
STAGE_TO_PHASE: dict[str, str] = {
    "FOUNDRY_INITIALIZED": "planning",
    "DISCOVERY": "planning",
    "SPEC_NORMALIZATION": "planning",
    "SPEC_REVIEW": "planning",
    "PLANNING": "planning",
    "PLAN_REVIEW": "planning",
    "PLAN_AUDIT": "planning",
    "PRE_EXECUTION_SNAPSHOT": "execution",
    "EXECUTION": "execution",
    "TEST_EXECUTION": "execution",
    "REPAIR": "execution",
    "CHECKPOINT_AUDIT": "audit",
    "AUDITING": "audit",
    "FINAL_AUDIT": "audit",
    "DONE": "completion",
    "FAILED": "completion",
}


def phase_for_stage(stage: str) -> str:
    """Return the event phase for a manifest stage. Defaults to 'execution'."""
    return STAGE_TO_PHASE.get(stage, "execution")


def _load_usage(task_dir: Path) -> dict:
    """Load existing token-usage.json or return empty structure."""
    path = task_dir / "token-usage.json"
    if path.exists():
        try:
            data = load_json(path)
            if isinstance(data, dict):
                for key, default in EMPTY_USAGE.items():
                    if key not in data:
                        data[key] = type(default)() if not isinstance(default, list) else []
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {k: ([] if isinstance(v, list) else type(v)()) for k, v in EMPTY_USAGE.items()}


def _recompute_totals(data: dict) -> None:
    """Recompute aggregate totals from by_agent and by_model dicts."""
    by_agent = data.get("by_agent", {})

    # Rebuild legacy agents dict
    data["agents"] = {
        name: info.get("tokens", info.get("input_tokens", 0) + info.get("output_tokens", 0))
        for name, info in by_agent.items()
        if isinstance(info, dict)
    }

    # Recompute by_model from by_agent (authoritative source)
    by_model: dict[str, dict] = {}
    for info in by_agent.values():
        if not isinstance(info, dict):
            continue
        model = _resolve_model(info.get("model", "unknown"))
        info["model"] = model  # fix in-place for legacy data
        if model in ("none", "n/a", ""):
            continue
        if model not in by_model:
            by_model[model] = {"input_tokens": 0, "output_tokens": 0, "tokens": 0}
        by_model[model]["input_tokens"] += info.get("input_tokens", 0)
        by_model[model]["output_tokens"] += info.get("output_tokens", 0)
        by_model[model]["tokens"] += info.get("tokens", 0)
    data["by_model"] = by_model

    # Totals (only count non-deterministic events in token totals)
    data["total"] = sum(v.get("tokens", 0) for v in by_agent.values() if isinstance(v, dict))
    data["total_input_tokens"] = sum(v.get("input_tokens", 0) for v in by_agent.values() if isinstance(v, dict))
    data["total_output_tokens"] = sum(v.get("output_tokens", 0) for v in by_agent.values() if isinstance(v, dict))


_KNOWN_MODELS = {"opus", "sonnet", "haiku"}
_DEFAULT_PARENT_MODEL = "opus"  # orchestrator always runs on opus


def _resolve_model(model: str) -> str:
    """Resolve 'default'/'null'/empty model names to the actual model.

    When the executor router returns model=null it means 'inherit from parent'.
    The parent (orchestrator) runs on opus, so 'default' → 'opus'.
    """
    if model in _KNOWN_MODELS:
        return model
    if model in ("none", "n/a"):
        return model  # deterministic events legitimately have no model
    # Everything else (default, null, empty, unknown) → parent model
    return _DEFAULT_PARENT_MODEL


def record_tokens(
    task_dir: Path,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    phase: str = "execution",
    stage: str = "",
    event_type: str = "spawn",
    segment: str | None = None,
    detail: str | None = None,
) -> dict:
    """Record a single event's token usage. Returns updated data."""
    task_dir = Path(task_dir)
    data = _load_usage(task_dir)

    model = _resolve_model(model)

    total = input_tokens + output_tokens
    now = datetime.now(timezone.utc).isoformat()

    # Append event to chronological log
    event: dict = {
        "timestamp": now,
        "agent": agent,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "tokens": total,
        "phase": phase,
        "stage": stage,
        "type": event_type,
    }
    if segment:
        event["segment"] = segment
    if detail:
        event["detail"] = detail
    # Validate phase/type — warn but still record
    warnings = []
    if phase not in VALID_PHASES:
        warnings.append(f"unknown phase: {phase}")
    if event_type not in VALID_TYPES:
        warnings.append(f"unknown type: {event_type}")
    if warnings:
        event["validation_warning"] = "; ".join(warnings)
    data.setdefault("events", []).append(event)

    # Only update aggregates for non-deterministic events with actual tokens
    if event_type != "deterministic" and total > 0:
        by_agent = data.setdefault("by_agent", {})
        if agent not in by_agent:
            by_agent[agent] = {"input_tokens": 0, "output_tokens": 0, "tokens": 0, "model": model}
        by_agent[agent]["input_tokens"] += input_tokens
        by_agent[agent]["output_tokens"] += output_tokens
        by_agent[agent]["tokens"] += total
        by_agent[agent]["model"] = model

    _recompute_totals(data)
    write_json(task_dir / "token-usage.json", data)
    return data


def get_summary(task_dir: Path) -> dict:
    """Return validated token summary for a task.

    Also writes back resolved model names so legacy 'default' entries
    get fixed on disk.
    """
    task_dir = Path(task_dir)
    data = _load_usage(task_dir)
    _recompute_totals(data)
    write_json(task_dir / "token-usage.json", data)
    return data


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Deterministic token usage tracking")
    sub = parser.add_subparsers(dest="command")

    rec = sub.add_parser("record", help="Record tokens from a subagent spawn or deterministic step")
    rec.add_argument("--task-dir", required=True, help="Path to .dynos/task-{id}")
    rec.add_argument("--agent", required=True, help="Agent or tool name (e.g. backend-executor-seg1, validate_task_artifacts)")
    rec.add_argument("--model", required=True, help="Model used (opus, sonnet, haiku, none)")
    rec.add_argument("--input-tokens", type=int, required=True, help="Input tokens (uploaded)")
    rec.add_argument("--output-tokens", type=int, required=True, help="Output tokens (downloaded)")
    rec.add_argument("--phase", default="execution", help="Pipeline phase: planning, execution, audit, tdd, repair")
    rec.add_argument("--stage", default="", help="Manifest stage (e.g. EXECUTION, AUDITING, SPEC_NORMALIZATION)")
    rec.add_argument("--type", default="spawn", dest="event_type", help="Event type: spawn, deterministic, inline")
    rec.add_argument("--segment", default=None, help="Execution graph segment ID (e.g. seg-1)")
    rec.add_argument("--detail", default=None, help="Human-readable detail of what happened")

    summ = sub.add_parser("summary", help="Print token usage summary as JSON")
    summ.add_argument("--task-dir", required=True, help="Path to .dynos/task-{id}")

    args = parser.parse_args()

    if args.command == "record":
        data = record_tokens(
            task_dir=Path(args.task_dir),
            agent=args.agent,
            model=args.model,
            input_tokens=max(0, args.input_tokens),
            output_tokens=max(0, args.output_tokens),
            phase=args.phase,
            stage=args.stage,
            event_type=args.event_type,
            segment=args.segment,
            detail=args.detail,
        )
        print(json.dumps({
            "ok": True,
            "total": data["total"],
            "total_input_tokens": data["total_input_tokens"],
            "total_output_tokens": data["total_output_tokens"],
            "events_count": len(data.get("events", [])),
        }))

    elif args.command == "summary":
        data = get_summary(Path(args.task_dir))
        print(json.dumps(data, indent=2))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
