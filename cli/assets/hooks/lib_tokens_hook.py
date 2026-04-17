#!/usr/bin/env python3
"""Parse subagent transcript and record token usage to active task.

Called by the SubagentStop hook after every subagent completes.
Reads the JSONL transcript, sums input/output tokens across all messages,
detects which model was used, and writes to the active task's token-usage.json.

Usage:
    python3 lib_tokens_hook.py \
        --transcript /path/to/agent-xxx.jsonl \
        --agent-type "dynos-work:backend-executor" \
        --agent-desc "Implement API route" \
        --root /path/to/project
"""

from __future__ import annotations

import sys as _sys; _sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

import argparse
import json
import re
import sys
from pathlib import Path

from lib_tokens import record_tokens


def _find_active_task(root: Path) -> Path | None:
    """Find the most recent active task directory."""
    dynos = root / ".dynos"
    if not dynos.exists():
        return None

    candidates = []
    for td in dynos.iterdir():
        if not td.is_dir() or not re.match(r"task-\d{8}-\d{3}$", td.name):
            continue
        manifest_path = td / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            stage = manifest.get("stage", "")
            if stage not in ("DONE", "FAILED"):
                candidates.append((td.name, td, stage))
        except (json.JSONDecodeError, OSError):
            continue

    if not candidates:
        return None

    # Return the most recent by task ID (lexicographic sort)
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _parse_transcript(transcript_path: Path) -> dict:
    """Parse a subagent transcript JSONL and sum token usage.

    Returns: {input_tokens, output_tokens, model, agent_id}
    """
    total_input = 0
    total_output = 0
    model = "unknown"
    agent_id = ""

    with open(transcript_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Extract agent ID
            if not agent_id and entry.get("agentId"):
                agent_id = entry["agentId"]

            # Extract model from message
            msg = entry.get("message", {})
            if isinstance(msg, dict):
                if msg.get("model"):
                    model = msg["model"]
                # Sum usage from each message
                usage = msg.get("usage", {})
                if isinstance(usage, dict):
                    total_input += usage.get("input_tokens", 0)
                    total_input += usage.get("cache_read_input_tokens", 0)
                    total_input += usage.get("cache_creation_input_tokens", 0)
                    total_output += usage.get("output_tokens", 0)

    # Normalize model name
    if "haiku" in model:
        model = "haiku"
    elif "sonnet" in model:
        model = "sonnet"
    elif "opus" in model:
        model = "opus"

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "model": model,
        "agent_id": agent_id,
    }


def _detect_phase_and_stage(agent_type: str, task_dir: Path) -> tuple[str, str]:
    """Infer phase and stage from agent type and current manifest."""
    # Read current stage from manifest
    stage = ""
    try:
        manifest = json.loads((task_dir / "manifest.json").read_text())
        stage = manifest.get("stage", "")
    except (json.JSONDecodeError, OSError):
        pass

    # Infer phase from agent type
    agent_lower = agent_type.lower()
    if "auditor" in agent_lower or "audit" in agent_lower:
        phase = "audit"
    elif "planning" in agent_lower or "planner" in agent_lower:
        phase = "planning"
    elif "repair" in agent_lower:
        phase = "repair"
    elif "testing" in agent_lower or "tdd" in agent_lower:
        phase = "tdd"
    elif "executor" in agent_lower:
        phase = "execution"
    elif "investigator" in agent_lower:
        phase = "audit"
    else:
        # Fall back to stage-based inference
        from lib_tokens import phase_for_stage
        phase = phase_for_stage(stage)

    return phase, stage


def _detect_segment(agent_type: str, agent_desc: str) -> str | None:
    """Try to extract segment ID from agent type or description."""
    for text in (agent_type, agent_desc):
        match = re.search(r"seg-?\d+", text, re.IGNORECASE)
        if match:
            return match.group(0)
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Record subagent token usage")
    parser.add_argument("--transcript", required=True, help="Path to subagent transcript JSONL")
    parser.add_argument("--agent-type", default="unknown", help="Agent type (e.g. dynos-work:backend-executor)")
    parser.add_argument("--agent-desc", default="", help="Agent description")
    parser.add_argument("--root", required=True, help="Project root path")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    transcript_path = Path(args.transcript)

    if not transcript_path.exists():
        return 0

    # Find active task
    task_dir = _find_active_task(root)
    if task_dir is None:
        return 0

    # Parse transcript
    result = _parse_transcript(transcript_path)

    # Skip if no tokens recorded (empty transcript)
    if result["input_tokens"] == 0 and result["output_tokens"] == 0:
        return 0

    # Build agent name from type
    agent_name = args.agent_type
    # Strip "dynos-work:" prefix for cleaner names
    if agent_name.startswith("dynos-work:"):
        agent_name = agent_name[len("dynos-work:"):]

    # Detect phase, stage, segment
    phase, stage = _detect_phase_and_stage(args.agent_type, task_dir)
    segment = _detect_segment(args.agent_type, args.agent_desc)

    # Record tokens
    record_tokens(
        task_dir=task_dir,
        agent=agent_name,
        model=result["model"],
        input_tokens=result["input_tokens"],
        output_tokens=result["output_tokens"],
        phase=phase,
        stage=stage,
        event_type="spawn",
        segment=segment,
        detail=args.agent_desc[:200] if args.agent_desc else None,
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
