"""Recompute quality_score on retrospectives scored by an older compute_reward.

Retrospectives written before ``REWARD_SCORING_VERSION`` 2 counted
superseded and overruled audit reports as surviving blocking findings, so a
task the findings gate cleared could carry ``quality_score: 0.0``. Postmortem
then raised ``low_quality`` from that stored value, and learning consumers
read it as a real score.

``rescore_task`` recomputes the score from the task directory with the
current ``compute_reward`` and persists it the way retrospectives are
written: the worktree ``task-retrospective.json`` (plus a task-receipt-chain
entry when the task has a chain) and, when the task was flushed at DONE, the
persistent copy with a fresh ``retrospective_flushed`` event so it stays
verified. Only the quality fields change: the streak fields depend on the
retrospectives that existed when the task finished and are left as they are.

When the task directory is gone the score cannot be recomputed.
``quality_unverified`` names the one case the old formula is known to get
wrong: an unversioned ``0.0`` on a DONE task, which reaches DONE only with the
gate clear. Consumers treat that score as unknown rather than low.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from lib_core import _flush_retrospective_on_done, _persistent_project_dir, load_json, now_iso, write_json
from lib_validate import REWARD_SCORING_VERSION, compute_reward

_TASK_ID_RE = re.compile(r"^task-[A-Za-z0-9][A-Za-z0-9_.-]*$")


def scoring_version(retro: dict) -> int:
    raw = retro.get("reward_scoring_version") if isinstance(retro, dict) else None
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 1


def is_stale(retro: dict) -> bool:
    return isinstance(retro, dict) and scoring_version(retro) < REWARD_SCORING_VERSION


def quality_unverified(retro: dict) -> bool:
    """True when the stored score is an old-formula 0.0 on a DONE task."""
    if not is_stale(retro):
        return False
    q = retro.get("quality_score")
    return (
        isinstance(q, (int, float)) and not isinstance(q, bool) and float(q) == 0.0
        and str(retro.get("task_outcome", "")).upper() == "DONE"
    )


def effective_quality(retro: dict) -> float | None:
    """The quality score to act on, or None when it is not trustworthy."""
    if quality_unverified(retro):
        return None
    q = retro.get("quality_score") if isinstance(retro, dict) else None
    if isinstance(q, (int, float)) and not isinstance(q, bool):
        return float(q)
    return None


def _rescorable(task_dir: Path, retro: dict) -> bool:
    if not (task_dir / "manifest.json").is_file():
        return False
    if (task_dir / "audit-reports").is_dir():
        return True
    # No reports on disk: only a task that never had findings can be rescored
    # (otherwise compute_reward would read "no findings" as perfect quality).
    findings = retro.get("findings_by_auditor")
    return isinstance(findings, dict) and not any(
        isinstance(v, (int, float)) and v > 0 for v in findings.values()
    )


def rescore_task(root: Path, task_id: str, *, persist: bool = True) -> dict:
    """Recompute a stale retrospective's quality_score.

    Returns ``{"task_id", "status", ...}`` where status is ``current``
    (already at the current version), ``rescored`` (score recomputed;
    ``previous``/``quality_score`` carry both values), ``unavailable`` (the
    task directory or its audit reports are gone) or ``missing`` (no
    worktree retrospective).
    """
    root = Path(root)
    if not isinstance(task_id, str) or not _TASK_ID_RE.match(task_id):
        return {"task_id": task_id, "status": "invalid_task_id"}
    task_dir = root / ".dynos" / task_id
    retro_path = task_dir / "task-retrospective.json"
    try:
        retro = load_json(retro_path)
    except (OSError, ValueError, json.JSONDecodeError):
        retro = None
    if not isinstance(retro, dict):
        return {"task_id": task_id, "status": "missing"}
    if not is_stale(retro):
        return {"task_id": task_id, "status": "current", "quality_score": retro.get("quality_score")}
    if not _rescorable(task_dir, retro):
        return {
            "task_id": task_id, "status": "unavailable",
            "quality_score": retro.get("quality_score"),
            "quality_unverified": quality_unverified(retro),
        }

    try:
        fresh = compute_reward(task_dir)
    except Exception as exc:  # noqa: BLE001 — a broken task dir must not break postmortem
        return {
            "task_id": task_id, "status": "error", "error": str(exc),
            "quality_score": retro.get("quality_score"),
            "quality_unverified": quality_unverified(retro),
        }
    previous = retro.get("quality_score")
    updated = dict(retro)
    updated["quality_score"] = fresh["quality_score"]
    updated["reward_scoring_version"] = REWARD_SCORING_VERSION
    updated["quality_score_rescore"] = {
        "previous": previous,
        "previous_scoring_version": scoring_version(retro),
        "rescored_at": now_iso(),
    }
    result = {
        "task_id": task_id, "status": "rescored",
        "previous": previous, "quality_score": fresh["quality_score"],
    }
    if not persist:
        return result

    persistent = _persistent_project_dir(root) / "retrospectives" / f"{task_id}.json"
    was_flushed = persistent.is_file()
    write_json(retro_path, updated)
    if _has_chain(task_dir):
        try:
            from lib_chain import extend_chain_for_artifact  # noqa: PLC0415
            extend_chain_for_artifact(task_dir, retro_path)
        except Exception as exc:  # noqa: BLE001 — chain extension is best effort, as in ctl
            _log(root, "task_receipt_chain_extension_failed", task_id, file=str(retro_path), error=str(exc))
    if was_flushed:
        try:
            manifest = load_json(task_dir / "manifest.json")
        except (OSError, ValueError):
            manifest = {}
        _flush_retrospective_on_done(task_dir=task_dir, manifest=manifest if isinstance(manifest, dict) else {})
    _log(root, "retrospective_rescored", task_id, previous=previous, quality_score=fresh["quality_score"],
         scoring_version=REWARD_SCORING_VERSION)
    return result


def _has_chain(task_dir: Path) -> bool:
    try:
        from lib_chain import _CHAIN_FILENAME  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return False
    return (task_dir / _CHAIN_FILENAME).exists()


def _log(root: Path, event: str, task_id: str, **payload: object) -> None:
    try:
        from lib_log import log_event  # noqa: PLC0415
        log_event(root, event, task=task_id, **payload)
    except Exception:  # noqa: BLE001
        pass


def rescore_all(root: Path, *, persist: bool = True) -> list[dict]:
    """``rescore_task`` for every worktree retrospective that is stale."""
    root = Path(root)
    results = []
    for path in sorted((root / ".dynos").glob("task-*/task-retrospective.json")):
        try:
            retro = load_json(path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(retro, dict) and is_stale(retro):
            results.append(rescore_task(root, path.parent.name, persist=persist))
    return results
