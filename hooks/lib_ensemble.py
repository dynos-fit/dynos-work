"""Deterministic ensemble cascade evaluation.

Single source of truth for the audit ensemble cascade
(fast tier -> balanced tier when no blocking findings -> deep tier on any
blocking finding; see ``_needs_escalation``).
Every enforcement point reads the cascade state from this module so the
orchestrator never has to interpret prose to decide which spawn comes next:

- ``ctl ensemble-next`` prints the next required spawn per auditor.
- ``router.py audit-inject-prompt`` refuses to build a prompt for an
  ensemble auditor at any model other than the next cascade step.
- ``receipt_audit_done`` refuses a shard receipt written out of cascade order.
- ``ctl run-audit-summary`` and the DONE gate
  (``lib_core._check_ensemble_voting``) refuse while a cascade is incomplete.

Cascade state is derived purely from the per-model shard receipts
``receipts/audit-{auditor}-{model}.json``. A shard for a later tier that is
older than the current fast-tier shard is treated as stale (the cascade was
restarted) and ignored.

Re-audit (``repair-log.json`` exists): the cascade does not restart. An
ensemble auditor is re-audited by ONE spawn at the escalation model, whose
verdict is binding (see ``evaluate_reaudit``). The deep tier usually raised
the repaired findings, so it is the one that judges the repairs; running the
fast and balanced tiers again only added serial spawns. The escalation shard
receipt written during a re-audit is stamped with the repair cycle by
``receipt_audit_done``; that stamp is what marks the re-audit as done for the
current cycle.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

STATUS_SPAWN = "spawn"
STATUS_COMPLETE = "complete"
STATUS_SINGLE = "single"
STATUS_INVALID = "invalid"

REASON_CASCADE_STEP = "cascade_step"
REASON_ESCALATE = "escalate_on_findings"
REASON_REAUDIT = "reaudit_deep_tier"

VERDICT_PASS = "pass"
VERDICT_ESCALATED = "escalated"


# ---------------------------------------------------------------------------
# Plan lookup
# ---------------------------------------------------------------------------

def _entries_from(obj: Any) -> list[dict]:
    if not isinstance(obj, dict):
        return []
    auditors = obj.get("auditors")
    if not isinstance(auditors, list):
        return []
    return [e for e in auditors if isinstance(e, dict)]


def load_plan_entries(task_dir: Path) -> list[dict]:
    """Return the auditor entries for a task.

    ``audit-plan.json`` (the router's output) is authoritative; the
    ``audit-routing`` receipt is the fallback for tasks whose plan file was
    not persisted. Returns an empty list when neither exists.
    """
    plan_path = task_dir / "audit-plan.json"
    if plan_path.exists():
        try:
            entries = _entries_from(json.loads(plan_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            entries = []
        if entries:
            return entries
    try:
        from lib_receipts import read_receipt  # noqa: PLC0415
        routing = read_receipt(task_dir, "audit-routing")
    except Exception:
        routing = None
    return _entries_from(routing)


def load_plan_entry(task_dir: Path, auditor_name: str) -> dict | None:
    for entry in load_plan_entries(task_dir):
        if entry.get("name") == auditor_name:
            return entry
    return None


# ---------------------------------------------------------------------------
# Cascade configuration
# ---------------------------------------------------------------------------

def cascade_models(entry: dict) -> tuple[list[str], str]:
    """Return ``(voting_models, escalation_model)`` from a plan entry.

    ``escalation_model`` is ``""`` when the entry does not configure one.
    """
    voting_raw = entry.get("ensemble_voting_models")
    if voting_raw is None:
        voting_raw = entry.get("voting_models")
    voting: list[str] = []
    if isinstance(voting_raw, list):
        voting = [m for m in voting_raw if isinstance(m, str) and m]
    escalation = entry.get("ensemble_escalation_model")
    if escalation is None:
        escalation = entry.get("escalation_model")
    if not isinstance(escalation, str):
        escalation = ""
    return voting, escalation


def is_ensemble_entry(entry: dict | None) -> bool:
    return isinstance(entry, dict) and entry.get("ensemble") is True


def reaudit_model_for(entry: dict) -> str:
    """The single model an ensemble auditor is re-audited at, or ``""``.

    Only an escalation model that sits above the voting tiers qualifies; when
    none is configured (or it is one of the voting tiers) a re-audit keeps the
    normal cascade.
    """
    voting, escalation = cascade_models(entry)
    if escalation and voting and escalation not in voting:
        return escalation
    return ""


def current_repair_cycle(task_dir: Path) -> int | None:
    """The repair cycle a re-audit belongs to, or ``None`` on a first audit.

    ``repair-log.json`` existing is what makes an audit a re-audit (the same
    test ``router._is_reaudit`` uses). An unreadable log fails open to the
    first-audit cascade rather than inventing a cycle number.
    """
    path = task_dir / "repair-log.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    cycle = data.get("repair_cycle", 1)
    if type(cycle) is not int or cycle < 1:
        return None
    return cycle


def _stamped_cycle(receipt: dict | None) -> int | None:
    """Repair cycle a shard receipt was written in (``None`` = first audit)."""
    if not isinstance(receipt, dict):
        return None
    cycle = receipt.get("repair_cycle")
    if type(cycle) is int and cycle >= 1:
        return cycle
    return None


# ---------------------------------------------------------------------------
# Receipt reading
# ---------------------------------------------------------------------------

def _receipt_ts(receipt: dict | None) -> str:
    if not isinstance(receipt, dict):
        return ""
    ts = receipt.get("ts")
    return ts if isinstance(ts, str) else ""


def read_cascade_receipts(
    task_dir: Path,
    auditor_name: str,
    voting: list[str],
    escalation: str,
) -> dict[str, dict | None]:
    """Read every shard receipt of the cascade, dropping stale later tiers.

    A later-tier (or escalation) shard whose ``ts`` is older than the
    fast-tier shard belongs to a previous run of the cascade and is ignored.
    """
    from lib_receipts import read_receipt  # noqa: PLC0415

    models: list[str] = list(voting)
    if escalation and escalation not in models:
        models.append(escalation)
    receipts: dict[str, dict | None] = {}
    for model in models:
        receipts[model] = read_receipt(
            task_dir, f"audit-{auditor_name}-{model}", min_version=2
        )
    if voting:
        fast_ts = _receipt_ts(receipts.get(voting[0]))
        if fast_ts:
            for model in models[1:]:
                shard = receipts.get(model)
                if shard is not None and _receipt_ts(shard) < fast_ts:
                    receipts[model] = None
    return receipts


def _finding_count(receipt: dict) -> int:
    try:
        return int(receipt.get("finding_count", -1))
    except (TypeError, ValueError):
        return -1


def _needs_escalation(shard: dict) -> bool:
    """Single escalation rule: blocking findings escalate to the deep tier.

    Uses ``blocking_count`` when it is a non-negative ``int`` (not a bool,
    string or float); otherwise falls back to ``_finding_count(shard) != 0``
    (missing/non-coercible ``finding_count`` yields -1 -> True, fail-closed).
    """
    blocking = shard.get("blocking_count")
    # Only a real non-negative int counts. ``receipt_audit_done`` always
    # writes an int; a bool, string or float here means the shard was not
    # written by it, so fall back to the stricter finding-count rule rather
    # than trusting a coerced value (``int("0")``, ``int(0.9)``, ``True``).
    if type(blocking) is int and blocking >= 0:
        return blocking > 0
    return _finding_count(shard) != 0


# ---------------------------------------------------------------------------
# Pure evaluation
# ---------------------------------------------------------------------------

def evaluate_cascade(
    auditor_name: str,
    entry: dict,
    receipts: dict[str, dict | None],
) -> dict:
    """Evaluate the cascade for one auditor from its shard receipts.

    Pure function: ``receipts`` maps model -> receipt dict (or ``None``).
    Returns a dict with at least ``status`` and ``gaps``:

    - ``status == "single"``: not an ensemble auditor; ``model`` is the plan
      model.
    - ``status == "spawn"``: the next required spawn is ``model``
      (``reason`` is ``cascade_step`` or ``escalate_on_findings``).
    - ``status == "complete"``: nothing more to spawn; ``verdict`` is
      ``pass`` (every voting tier clean) or ``escalated`` (deep tier ran).
    - ``status == "invalid"``: the plan entry cannot be evaluated.

    ``gaps`` lists protocol violations visible in the receipts (an
    escalation shard without the fast-tier shard, a receipt whose
    ``model_used`` is outside the cascade). ``gaps`` is non-empty whenever
    the cascade would not satisfy the DONE gate.
    """
    base: dict[str, Any] = {
        "auditor": auditor_name,
        "ensemble": is_ensemble_entry(entry),
        "gaps": [],
    }
    if not is_ensemble_entry(entry):
        plan_model = entry.get("model") if isinstance(entry, dict) else None
        return {**base, "status": STATUS_SINGLE, "model": plan_model}

    voting, escalation = cascade_models(entry)
    base.update({"voting_models": list(voting), "escalation_model": escalation})
    gaps: list[str] = base["gaps"]

    if not voting:
        gaps.append(f"auditor {auditor_name} ensemble=true but voting_models is empty")
        return {**base, "status": STATUS_INVALID, "model": None}

    allowed = set(voting)
    if escalation:
        allowed.add(escalation)
    for _model, shard in receipts.items():
        if shard is None:
            continue
        model_used = shard.get("model_used")
        if isinstance(model_used, str) and model_used and model_used not in allowed:
            gaps.append(
                f"auditor {auditor_name} receipt model_used={model_used} not in voting set"
            )

    escalation_receipt = receipts.get(escalation) if escalation else None

    def _spawn(model: str, tier_index: int | None, reason: str, **extra: Any) -> dict:
        if reason == REASON_CASCADE_STEP:
            missing = [m for m in voting if receipts.get(m) is None]
            gaps.append(
                f"auditor {auditor_name} ensemble missing voting-model receipt(s): "
                f"{', '.join(missing)} (next cascade step: {model}); "
                f"expected receipt: receipts/audit-{auditor_name}-{model}.json "
                f"(shard_step_name {auditor_name}-{model})"
            )
        else:
            gaps.append(
                f"auditor {auditor_name} ensemble voting-model receipts found issues "
                f"(blocking findings) and no escalation receipt for {model!r}; "
                f"expected receipt: receipts/audit-{auditor_name}-{model}.json "
                f"(shard_step_name {auditor_name}-{model})"
            )
        return {
            **base,
            "status": STATUS_SPAWN,
            "model": model,
            "tier_index": tier_index,
            "reason": reason,
            "shard_step_name": f"{auditor_name}-{model}",
            **extra,
        }

    for index, model in enumerate(voting):
        shard = receipts.get(model)
        if shard is None:
            if escalation_receipt is not None:
                gaps.append(
                    f"auditor {auditor_name} has an escalation receipt for "
                    f"{escalation!r} but no voting-tier receipt for {model}; "
                    "the cascade must run the voting tiers first"
                )
            return _spawn(model, index, REASON_CASCADE_STEP)
        if _needs_escalation(shard):
            if not escalation:
                gaps.append(
                    f"auditor {auditor_name} voting-model receipt {model} found "
                    "issues but no escalation model is configured"
                )
                return {**base, "status": STATUS_INVALID, "model": None}
            if escalation_receipt is None:
                return _spawn(escalation, None, REASON_ESCALATE, triggered_by=model)
            return {
                **base,
                "status": STATUS_COMPLETE,
                "model": None,
                "verdict": VERDICT_ESCALATED,
                "triggered_by": model,
            }

    return {**base, "status": STATUS_COMPLETE, "model": None, "verdict": VERDICT_PASS}


def evaluate_reaudit(
    auditor_name: str,
    entry: dict,
    receipts: dict[str, dict | None],
    escalation_receipt: dict | None,
    cycle: int,
) -> dict:
    """Evaluate an ensemble auditor during a re-audit (repair cycle ``cycle``).

    A re-audit is one spawn at the escalation model (``reaudit_model``), never
    a fresh fast -> balanced -> deep cascade. Pure function:
    ``receipts`` is the stale-filtered cascade view (``read_cascade_receipts``)
    and ``escalation_receipt`` the escalation shard as written on disk.

    - Escalation shard stamped with ``cycle``: complete, ``escalated``; the
      re-audit ran and its deep-tier verdict is binding.
    - Escalation shard stamped with another cycle: an earlier re-audit's
      deep-tier verdict is binding (``escalated``); this cycle's re-audit
      spawn is still available.
    - No stamped shard: the first audit's cascade decides. A complete cascade
      stays complete (the binding reports are the ones it produced). An
      incomplete one is finished by the single re-audit spawn, not by more
      voting tiers.

    Every result carries ``reaudit_cycle``, ``reaudit_model`` and
    ``reaudit_done``.
    """
    reaudit_model = reaudit_model_for(entry)
    extra = {
        "reaudit_cycle": cycle,
        "reaudit_model": reaudit_model,
        "reaudit_shard_step_name": f"{auditor_name}-{reaudit_model}",
    }
    voting, escalation = cascade_models(entry)
    base: dict[str, Any] = {
        "auditor": auditor_name,
        "ensemble": True,
        "voting_models": list(voting),
        "escalation_model": escalation,
        "gaps": [],
    }
    stamped = _stamped_cycle(escalation_receipt)
    if stamped is not None:
        return {
            **base, **extra,
            "status": STATUS_COMPLETE,
            "model": None,
            "verdict": VERDICT_ESCALATED,
            "reaudit_done": stamped == cycle,
        }
    history = evaluate_cascade(auditor_name, entry, receipts)
    if history["status"] == STATUS_SPAWN:
        return {
            **base, **extra,
            "status": STATUS_SPAWN,
            "model": reaudit_model,
            "tier_index": None,
            "reason": REASON_REAUDIT,
            "shard_step_name": f"{auditor_name}-{reaudit_model}",
            "reaudit_done": False,
            "gaps": [
                f"auditor {auditor_name} re-audit (repair cycle {cycle}) needs one "
                f"{reaudit_model!r} spawn; expected receipt: "
                f"receipts/audit-{auditor_name}-{reaudit_model}.json "
                f"(shard_step_name {auditor_name}-{reaudit_model})"
            ],
        }
    return {**history, **extra, "reaudit_done": False}


# ---------------------------------------------------------------------------
# Task-dir entry points
# ---------------------------------------------------------------------------

def ensemble_next(task_dir: Path, auditor_name: str, entry: dict | None = None) -> dict:
    """Evaluate one auditor's cascade against the receipts on disk.

    During a re-audit (``repair-log.json`` exists) an ensemble auditor with an
    escalation model is evaluated by ``evaluate_reaudit`` instead.
    """
    if entry is None:
        entry = load_plan_entry(task_dir, auditor_name)
    if entry is None:
        return {
            "auditor": auditor_name,
            "ensemble": False,
            "status": STATUS_INVALID,
            "model": None,
            "gaps": [f"auditor {auditor_name} not found in audit-plan.json or audit-routing receipt"],
        }
    if not is_ensemble_entry(entry):
        return evaluate_cascade(auditor_name, entry, {})
    voting, escalation = cascade_models(entry)
    receipts = read_cascade_receipts(task_dir, auditor_name, voting, escalation)
    cycle = current_repair_cycle(task_dir)
    if cycle is not None and reaudit_model_for(entry):
        from lib_receipts import read_receipt  # noqa: PLC0415

        escalation_receipt = read_receipt(
            task_dir, f"audit-{auditor_name}-{escalation}", min_version=2
        )
        return evaluate_reaudit(auditor_name, entry, receipts, escalation_receipt, cycle)
    return evaluate_cascade(auditor_name, entry, receipts)


def ensemble_next_all(task_dir: Path, entries: list[dict] | None = None) -> dict:
    """Evaluate every spawn-action auditor of a task.

    Returns ``{"auditors": [...], "pending": [names], "complete": bool}``.
    ``complete`` is true only when no ensemble auditor still needs a spawn
    and no cascade carries a gap.
    """
    if entries is None:
        entries = load_plan_entries(task_dir)
    results: list[dict] = []
    pending: list[str] = []
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if entry.get("action") != "spawn":
            continue
        result = ensemble_next(task_dir, name, entry)
        results.append(result)
        if result["status"] in (STATUS_SPAWN, STATUS_INVALID) or result["gaps"]:
            pending.append(name)
    return {"auditors": results, "pending": pending, "complete": not pending}


def cascade_gaps(task_dir: Path, entries: list[dict]) -> list[str]:
    """DONE-gate view: every gap across all ensemble spawn entries."""
    gaps: list[str] = []
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        if entry.get("action") != "spawn" or not is_ensemble_entry(entry):
            continue
        gaps.extend(ensemble_next(task_dir, name, entry)["gaps"])
    return gaps
