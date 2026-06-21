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
import os
import re
import sys
import time
from pathlib import Path

import lib_host
import actor_identity
from lib_models import valid_models_for_host
from lib_tokens import record_tokens


# Default window for treating a task as "actively receiving work."
# A task whose manifest hasn't been touched in this many seconds is considered
# stalled and will NOT be auto-attributed for SubagentStop tokens. Configurable
# via DYNOS_TASK_ATTRIBUTION_WINDOW_SECONDS for very long-running stages.
_DEFAULT_ATTRIBUTION_WINDOW_SECONDS = 3600  # 1 hour


def _attribution_window_seconds() -> float:
    """Return the active-task freshness window from env or default."""
    raw = os.environ.get("DYNOS_TASK_ATTRIBUTION_WINDOW_SECONDS")
    if raw:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return float(_DEFAULT_ATTRIBUTION_WINDOW_SECONDS)


def _active_tasks_from_manifests(dynos: Path) -> list[tuple[float, Path]]:
    candidates: list[tuple[float, Path]] = []
    for td in dynos.iterdir():
        if not td.is_dir() or not re.match(r"task-\d{8}-\d{3}$", td.name):
            continue
        manifest_path = td / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            stage = manifest.get("stage", "")
            if stage in ("DONE", "FAILED", "CANCELLED", "CALIBRATED"):
                continue
            candidates.append((manifest_path.stat().st_mtime, td))
        except (json.JSONDecodeError, OSError):
            continue
    return candidates


def _find_active_task(root: Path, session_id: str = "") -> Path | None:
    """Find the active task that should receive SubagentStop token attribution.

    Selection rules (in order):
    1. Fast path: use the hook-owned session->task binding when session_id is
       available.
    2. Legacy path: read .dynos/active-task.json only when it is unambiguous
       because there is at most one active task.
    3. Fallback: full O(N) scan of all task manifests, for backward compat
       with repos that pre-date the pointer file or when the pointer is absent.
    4. Skip tasks whose manifest is in a terminal stage (DONE, FAILED, CANCELLED).
    5. Among remaining, pick the task whose manifest.json was most recently
       modified (freshest mtime). An actively-progressing task always has a
       fresh manifest because transition_task rewrites it atomically.
    6. If even the freshest manifest is older than the attribution window
       (default 1h, override via DYNOS_TASK_ATTRIBUTION_WINDOW_SECONDS),
       return None to avoid mis-attributing unrelated subagents to a stalled
       task.
    """
    dynos = root / ".dynos"
    if not dynos.exists():
        return None

    if session_id:
        task_dir = actor_identity.lookup_session_task(root, session_id)
        if task_dir is not None:
            return task_dir

    candidates = _active_tasks_from_manifests(dynos)

    # ---- Legacy path: active-task pointer, only when unambiguous ----
    pointer_path = dynos / "active-task.json"
    if pointer_path.exists():
        try:
            pointer = json.loads(pointer_path.read_text())
            task_id = pointer.get("task_id")
            if task_id is None:
                return None  # Pointer explicitly cleared (terminal stage).
            task_dir_str = pointer.get("task_dir")
            fresh_candidates = [
                (mtime, td)
                for mtime, td in candidates
                if time.time() - mtime <= _attribution_window_seconds()
            ]
            if len(fresh_candidates) > 1:
                return None
            if (
                len(fresh_candidates) == 1
                and task_dir_str
                and fresh_candidates[0][1].resolve() != Path(task_dir_str).resolve()
            ):
                return None
            if task_dir_str:
                td = Path(task_dir_str)
                manifest_path = td / "manifest.json"
                if manifest_path.exists():
                    manifest = json.loads(manifest_path.read_text())
                    stage = manifest.get("stage", "")
                    if stage not in ("DONE", "FAILED", "CANCELLED"):
                        mtime = manifest_path.stat().st_mtime
                        age_seconds = time.time() - mtime
                        if age_seconds <= _attribution_window_seconds():
                            return td
            return None
        except (json.JSONDecodeError, OSError, ValueError):
            pass  # Fall through to full scan.

    # ---- Fallback: full O(N) scan ----
    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    fresh_candidates = [
        (mtime, td)
        for mtime, td in candidates
        if time.time() - mtime <= _attribution_window_seconds()
    ]
    if not fresh_candidates:
        return None  # All active tasks are stale; refuse attribution.
    if len(fresh_candidates) > 1:
        return None  # Ambiguous concurrent active tasks; require session binding.
    _, newest_dir = fresh_candidates[0]
    return newest_dir


def _parse_transcript(transcript_path: Path, host: str = "claude") -> dict:
    """Parse a subagent transcript JSONL and sum token usage.

    Args:
        transcript_path: Path to the transcript file (JSONL or JSON array).
        host: Host identifier used to validate the model name against
              valid_models_for_host(host). Defaults to "claude".

    Returns: {input_tokens, output_tokens, model, agent_id}

    When the detected model string is not in valid_models_for_host(host)
    AND both input_tokens == 0 and output_tokens == 0, the model field is
    set to the terminal sentinel "host_unsupported" (NARROW condition —
    do not widen; real usage with unrecognized models is NOT marked).
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
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue

            # Normalize: if the line parsed as a JSON array (e.g. a JSON file
            # written as a single array rather than JSONL), iterate its items.
            if isinstance(parsed, list):
                entries = parsed
            else:
                entries = [parsed]

            for entry in entries:
                if not isinstance(entry, dict):
                    continue

                # Extract agent ID from top-level agentId field (JSONL hook format).
                if not agent_id and entry.get("agentId"):
                    agent_id = entry["agentId"]

                # Try to extract model and usage from the "message" sub-object
                # (JSONL hook format) or directly from the entry itself
                # (plain JSON transcript format used in tests).
                msg = entry.get("message", {})
                if isinstance(msg, dict) and msg:
                    candidate = msg
                else:
                    candidate = entry

                if candidate.get("model"):
                    model = candidate["model"]

                # Sum usage from each message
                usage = candidate.get("usage", {})
                if isinstance(usage, dict):
                    total_input += usage.get("input_tokens", 0)
                    total_input += usage.get("cache_read_input_tokens", 0)
                    total_input += usage.get("cache_creation_input_tokens", 0)
                    total_output += usage.get("output_tokens", 0)

    # Normalize model name to short tier identifier.
    # Use the host's known models list (sorted for deterministic order) so
    # this block does not need to enumerate model family names literally.
    # A model string contains at most one family name, so the loop order
    # is immaterial — the first (and only) match wins identically to the
    # prior per-family if/elif chain.
    for _known in sorted(valid_models_for_host(host)):
        if _known in model:
            model = _known
            break

    # NARROW host_unsupported condition:
    # Only when BOTH token counts are zero AND the model is unrecognized for
    # this host. Do NOT widen — real usage with unrecognized models is kept as-is.
    if total_input == 0 and total_output == 0:
        valid = valid_models_for_host(host)
        if model not in valid:
            model = "host_unsupported"

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "model": model,
        "agent_id": agent_id,
    }


def _resolve_active_host(root: Path) -> str:
    """Resolve the active host for this hook invocation.

    Reads the persisted host from <root>/.dynos/control-plane.json
    (written by the host-detection bootstrap). Falls back to
    lib_host.detect_host() (env-based) when the control-plane file is
    absent, unreadable, or lacks a host key. The fallback default is
    "claude", so repos with no control-plane.json behave byte-identically
    to the prior hardcoded host="claude".
    """
    persisted = lib_host.get_persisted_host(root / ".dynos" / "control-plane.json")
    if persisted:
        return persisted
    return lib_host.detect_host()


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


def _record_orphan_tokens(
    root: Path,
    *,
    agent_name: str,
    agent_desc: str,
    result: dict,
    transcript_path: Path,
    reason: str,
) -> None:
    """Append a token record to .dynos/orphan-tokens.jsonl when no fresh
    active task is available for attribution.

    This is the safety net for the freshness gate in _find_active_task:
    instead of silently dropping legitimate token usage from long-running
    subagents (or subagents that finished after a long manual pause), the
    record is preserved in a visible orphan ledger. Operators can
    reconcile manually, and a future improvement could attribute orphans
    back to the right task by matching agent_id / transcript_path.
    """
    orphan_dir = root / ".dynos"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    orphan_path = orphan_dir / "orphan-tokens.jsonl"
    record = {
        "recorded_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "agent": agent_name,
        "agent_desc": agent_desc[:200] if agent_desc else "",
        "model": result.get("model", "unknown"),
        "input_tokens": result.get("input_tokens", 0),
        "output_tokens": result.get("output_tokens", 0),
        "agent_id": result.get("agent_id", ""),
        "transcript_path": str(transcript_path),
        "reason": reason,
    }
    try:
        with open(orphan_path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError:
        # Last resort: don't crash the SubagentStop hook if disk write fails.
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Record subagent token usage")
    parser.add_argument("--transcript", required=True, help="Path to subagent transcript JSONL")
    parser.add_argument("--agent-type", default="unknown", help="Agent type (e.g. dynos-work:backend-executor)")
    parser.add_argument("--agent-desc", default="", help="Agent description")
    parser.add_argument("--root", required=True, help="Project root path")
    parser.add_argument("--session-id", default="", help="Host session id for task attribution")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    transcript_path = Path(args.transcript)

    if not transcript_path.exists():
        return 0

    # Build agent name from type (used in both happy path and orphan path)
    agent_name = args.agent_type
    if agent_name.startswith("dynos-work:"):
        agent_name = agent_name[len("dynos-work:"):]

    # Resolve the active host once per hook invocation. Persisted host (from
    # .dynos/control-plane.json) wins; env-based detect_host() is the fallback,
    # whose default is "claude" — preserving prior byte-identical behavior.
    _active_host = _resolve_active_host(root)

    # Find active task. If no fresh non-terminal task exists, the tokens
    # would otherwise be silently dropped — record to orphan-tokens.jsonl
    # so the data is preserved for reconciliation.
    task_dir = _find_active_task(root, session_id=args.session_id)
    if task_dir is None:
        result = _parse_transcript(transcript_path, host=_active_host)
        if result["input_tokens"] > 0 or result["output_tokens"] > 0:
            _record_orphan_tokens(
                root,
                agent_name=agent_name,
                agent_desc=args.agent_desc,
                result=result,
                transcript_path=transcript_path,
                reason="no fresh active task at SubagentStop time",
            )
        return 0

    # Parse transcript
    result = _parse_transcript(transcript_path, host=_active_host)

    # Skip if no tokens recorded (empty transcript).
    # Exception: a zero-token transcript whose model resolved to the terminal
    # "host_unsupported" sentinel is NOT a mere empty transcript — it is the
    # circuit-breaker signal that the active host produced no recognizable
    # model attribution (binding decision 9). Record it so the sentinel can
    # fire downstream instead of being silently dropped here.
    if result["input_tokens"] == 0 and result["output_tokens"] == 0:
        if result["model"] != "host_unsupported":
            return 0

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
