# Write Boundary Spec

## Purpose

This spec defines a hard boundary between:

- LLM-authored work artifacts
- code-authored control-plane artifacts

The goal is to stop prompt drift, forged receipts, direct stage mutation, and "manual fallback" control behavior without taking real planning and implementation work away from the LLM.

The LLM should keep doing:

- task interpretation
- planning
- execution decomposition
- repair planning
- code/test/doc authoring

The framework should own:

- state transitions
- receipts
- gate outputs
- handoff records
- routing/calibration/policy state
- all proof that a deterministic step actually happened

## Core Principle

The correct split is not "LLM writes files" versus "code writes files."

The correct split is:

- LLM writes judgment artifacts
- code writes governance artifacts

Examples:

- `plan.md` is judgment
- `manifest.json.stage` is governance
- `execution-graph.json` is judgment
- `receipts/*.json` are governance
- `repair-log.json` is judgment
- `handoff-*.json` is governance

This spec turns that distinction into filesystem policy.

## Goals

1. Prevent LLMs from directly mutating control-plane files.
2. Reduce "trust me bro" orchestration paths.
3. Move dangerous writes behind deterministic wrappers.
4. Make prompt drift non-fatal by enforcing boundaries in code.
5. Preserve LLM ownership of actual work products.

## Non-Goals

1. This does not remove the LLM from planning, execution, or repair decisions.
2. This does not try to make plans or repairs fully deterministic.
3. This does not replace validators with prompt guidance.
4. This does not require a single-step refactor of the whole framework.

## Artifact Classes

### Class A: LLM-Owned Work Artifacts

These are the intended outputs of reasoning and implementation.

Examples:

- `.dynos/task-*/spec.md`
- `.dynos/task-*/plan.md`
- `.dynos/task-*/execution-graph.json`
- `.dynos/task-*/repair-log.json`
- `.dynos/task-*/audit-reports/*.json`
- `.dynos/task-*/evidence/*.md`
- task-related source code, tests, docs, configs

These are allowed to remain LLM-authored, but some may later move to wrapper-required mode.

### Class B: Code-Owned Control-Plane Artifacts

These must never be written directly by LLM paths.

Examples:

- `.dynos/task-*/manifest.json`
- `.dynos/task-*/receipts/*.json`
- `.dynos/task-*/handoff-*.json`
- `.dynos/task-*/external-solution-gate.json`
- `.dynos/task-*/token-usage.json`
- `.dynos/task-*/events.jsonl`
- scheduler outputs
- calibration outputs
- policy/routing/persistent state

These files are owned only by deterministic framework code.

### Class C: Wrapper-Required Hybrid Artifacts

These are still conceptually LLM-authored, but should be persisted through deterministic wrappers instead of direct model writes.

Recommended targets:

- `.dynos/task-*/execution-graph.json`
- `.dynos/task-*/repair-log.json`
- classification payload, ideally outside `manifest.json`

The model may generate content for these artifacts, but code should:

- validate schema
- normalize fields
- atomically persist the final file

## Proposed Architecture

Add a central enforcement module:

- `hooks/write_policy.py`

This module decides whether a given write attempt is:

- allowed directly
- denied
- allowed only through a wrapper

### Proposed Data Model

```python
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class WriteAttempt:
    role: str
    task_dir: Path | None
    path: Path
    operation: Literal["create", "modify", "delete"]
    source: Literal[
        "agent",
        "inline",
        "ctl",
        "scheduler",
        "receipt-writer",
        "eventbus",
        "system",
    ]


@dataclass(frozen=True)
class WriteDecision:
    allowed: bool
    reason: str
    mode: Literal["direct", "wrapper", "deny"]
    wrapper_command: str | None = None
```

### Proposed API

```python
def decide_write(attempt: WriteAttempt) -> WriteDecision: ...

def require_write_allowed(attempt: WriteAttempt) -> None: ...

def is_control_plane_path(path: Path, task_dir: Path | None) -> bool: ...

def allowed_globs_for_role(role: str, task_dir: Path | None) -> list[str]: ...
```

### Enforcement

The unconditional Class B control-plane guarantee is enforced inside `decide_write` in `hooks/write_policy.py` via three internal helpers:

- **`_owning_task_dir(path: Path) -> Path | None`** — computes the task directory that *owns* a given path based on the path itself, independent of the caller's bound `task_dir`. This means an executor bound to `task-A` cannot claim that a path under `task-B` belongs to its own task dir.

- **`_is_cross_task_control_plane(rel_posix: str) -> bool`** — classifies the target path by its own task-relative name (case-folded). Returns `True` if the relative name matches a Class B control-plane filename (e.g., `manifest.json`, `receipts/`, `handoff-*.json`). The case-fold prevents bypasses like `MANIFEST.JSON`.

- **`_FRAMEWORK_ROLES`** — a `frozenset` of roles (`ctl`, `scheduler`, `receipt-writer`, `eventbus`, `system`) that are unconditionally exempt from cross-task control-plane denial. These are the only roles whose writes bypass `_owning_task_dir` and `_is_cross_task_control_plane` checking inside `decide_write`.

Together, these three pieces ensure that the Class B guarantee is unconditional for non-framework roles: even if a role's prompt is compromised and it claims a different `task_dir`, the path-derived `_owning_task_dir` logic and name-based `_is_cross_task_control_plane` classify the write correctly.

## Role-Based Policy

Policy should be defined by framework role, not by prompt text.

Initial role set:

- `planning`
- `repair-coordinator`
- `audit-*`
- `execute-inline`
- `backend-executor`
- `ui-executor`
- `db-executor`
- `testing-executor`
- `integration-executor`
- `refactor-executor`
- `docs-executor`
- `ml-executor`
- `ctl`
- `scheduler`
- `receipt-writer`
- `eventbus`
- `system`

## Initial Allowlist Matrix

### Planning

Allowed:

- `.dynos/task-*/discovery-notes.md`
- `.dynos/task-*/design-decisions.md`
- `.dynos/task-*/spec.md`
- `.dynos/task-*/plan.md`
- `.dynos/task-*/execution-graph.json`

Denied:

- `.dynos/task-*/manifest.json`
- `.dynos/task-*/receipts/**`
- `.dynos/task-*/handoff-*.json`
- `.dynos/task-*/external-solution-gate.json`
- `.dynos/task-*/token-usage.json`
- `.dynos/task-*/events.jsonl`

### Repair Coordinator

Allowed:

- `.dynos/task-*/repair-log.json`

Denied:

- everything else under `.dynos/task-*`

### Auditors

Allowed:

- `.dynos/task-*/audit-reports/*.json`

Denied:

- `manifest.json`
- `repair-log.json`
- `receipts/**`
- `handoff-*.json`
- `spec.md`
- `plan.md`
- `execution-graph.json`

### Executors

Allowed:

- task-owned source/test/doc/config files
- `.dynos/task-*/evidence/*.md`

Denied:

- `manifest.json`
- `receipts/**`
- `audit-reports/*.json`
- `repair-log.json`
- `handoff-*.json`
- `external-solution-gate.json`

### Ctl

Allowed:

- code-owned control-plane files explicitly managed by commands

Examples:

- `manifest.json`
- `external-solution-gate.json`
- `handoff-*.json`
- future wrapped artifacts

### Receipt Writer

Allowed:

- `.dynos/task-*/receipts/*.json`

### Scheduler/Eventbus/System

Allowed:

- only their explicitly owned control-plane files and side effects

## File Ownership Matrix

### Freeze Immediately as Code-Owned

- `manifest.json`
- `receipts/**`
- `external-solution-gate.json`
- `handoff-*.json`
- `token-usage.json`
- `events.jsonl`
- calibration outputs
- policy/routing outputs

### Move to Wrapper-Required

- `execution-graph.json`
- `repair-log.json`
- classification payload persistence

## Enforcement Points

There are three enforcement layers.

### 1. Prompt Guidance

Useful, but insufficient.

Prompt guidance may explain the policy, but cannot be the real enforcement boundary.

### 2. Wrapper-Level Enforcement

Every deterministic writer checks write policy before touching disk.

Examples:

- `ctl.py`
- receipt writers
- scheduler
- eventbus-owned writers

This is the first real enforcement layer.

### 3. Patch/Apply Enforcement

Before model-generated file edits are applied:

- extract touched paths
- resolve active role
- validate against allowlist
- reject disallowed writes before disk mutation

This is the strongest end state.

## Recommended Persistence Wrappers

### Execution Graph

Proposed command:

```text
python3 hooks/ctl.py write-execution-graph .dynos/task-{id} --from tmp.json
```

Responsibilities:

- validate schema
- normalize segment order
- validate segment ids
- normalize paths
- stamp correct `task_id`
- atomically write file

### Repair Log

Proposed command:

```text
python3 hooks/ctl.py write-repair-log .dynos/task-{id} --from tmp.json
```

Responsibilities:

- validate schema
- normalize batch ordering
- normalize file field shape
- dedupe findings
- cross-check live audit finding ids
- cross-check auditor/severity where applicable
- atomically write file

### Classification

Recommended longer-term shape:

- planner writes `classification.json`
- deterministic code validates/normalizes it
- runtime state reads normalized classification instead of trusting freeform manifest mutation

Proposed command:

```text
python3 hooks/ctl.py write-classification .dynos/task-{id} --from tmp.json
```

## Normalization Rules

### Execution Graph Normalization

- segment ids must be normalized or rejected
- duplicate segments rejected
- duplicate `files_expected` entries removed or rejected
- task id stamped from manifest
- paths normalized to repo-relative form only
- `criteria_ids` must be integer lists

### Repair Log Normalization

- batch ids unique
- `affected_files` canonical key
- `finding_id` uniqueness
- retry/max-retry defaults applied
- status enum normalized
- model enum normalized
- live audit cross-check applied

### Classification Normalization

- `type` must be valid enum
- `risk_level` must be valid enum
- domains deduped
- empty strings removed
- derived flags like `fast_track` and `tdd_required` computed by code

## Atomic Write Contract

Any code-owned or wrapper-owned persistence path must:

1. validate before write
2. write to temporary file
3. use atomic replace
4. emit structured event
5. never partially write JSON

## Observability Contract

Every denied or wrapper-required write should emit a structured event.

### Denied Write Event

```json
{
  "event": "write_policy_denied",
  "role": "planning",
  "task_id": "task-20260421-001",
  "path": ".dynos/task-20260421-001/manifest.json",
  "operation": "modify",
  "reason": "manifest.json is code-owned control-plane state"
}
```

### Wrapper-Required Event

```json
{
  "event": "write_policy_wrapper_required",
  "role": "repair-coordinator",
  "task_id": "task-20260421-001",
  "path": ".dynos/task-20260421-001/repair-log.json",
  "operation": "create",
  "reason": "repair-log.json must be persisted through ctl wrapper"
}
```

### Allowed Write Event

Optional, but useful for audits and debugging:

```json
{
  "event": "write_policy_allowed",
  "role": "backend-executor",
  "task_id": "task-20260421-001",
  "path": "src/service/auth.py",
  "operation": "modify",
  "mode": "direct"
}
```

## Testing Strategy

Add four test families.

### 1. Policy Matrix Tests

Examples:

- planning cannot write `manifest.json`
- auditor cannot write `repair-log.json`
- executor cannot write `receipts/*.json`
- receipt writer can write `receipts/*.json`

### 2. Wrapper-Required Tests

Examples:

- direct write to `repair-log.json` denied
- `ctl write-repair-log` succeeds
- direct write to `execution-graph.json` denied once wrapper mode is enabled

### 3. End-to-End Flow Tests

Examples:

- start flow still succeeds
- execute flow still succeeds
- audit flow still succeeds
- no control-plane file is directly written by an agent role

### 4. Prompt/Policy Regression Tests

Examples:

- no prompt tells the model to mutate `manifest.json` directly
- no prompt tells the model to hand-write receipts
- no prompt tells the model to hand-write handoff/gate JSON

## Rollout Plan

This should be implemented in loops.

### Loop 1: Inventory and Freeze

Deliverables:

- add `hooks/write_policy.py`
- add path classification
- add role allowlists
- deny direct writes to obvious control-plane files

Exit criteria:

- no direct LLM writes to `manifest.json`
- no direct LLM writes to `receipts/**`
- no direct LLM writes to `handoff-*.json`
- no direct LLM writes to `external-solution-gate.json`

### Loop 2: Wrapper-Required Artifacts

Deliverables:

- `ctl write-execution-graph`
- `ctl write-repair-log`
- optional `ctl write-classification`
- normalization + atomic persistence

Exit criteria:

- hybrid artifacts no longer rely on direct model write
- wrappers validate before persistence

### Loop 3: Patch/Apply Gate

Deliverables:

- intercept model patch paths before disk apply
- reject disallowed targets centrally

Exit criteria:

- prompt drift cannot bypass write policy

### Loop 4: Semantic Hardening

Deliverables:

- graph vs plan consistency checks
- repair-log vs audit evidence checks
- classification normalization

Exit criteria:

- wrappers validate semantics, not just schema

### Loop 5: Observability and Dashboarding

Deliverables:

- denial events visible
- wrapper-required events visible
- per-task write-boundary diagnostics

Exit criteria:

- framework can show where models are still trying to cheat

## Open Decisions

These decisions should be made explicitly.

### 1. Should `execution-graph.json` remain direct-write?

Recommendation:

- no
- move to wrapper-required

### 2. Should `repair-log.json` remain direct-write?

Recommendation:

- no
- move to wrapper-required

### 3. Should classification remain embedded in `manifest.json`?

Recommendation:

- not long-term
- move to dedicated classification artifact plus deterministic persistence

### 4. Should enforcement start as deny-only or patch-intercept?

Recommendation:

- start deny-only on control-plane files
- then add patch interception

## Success Criteria

This work is complete when:

1. LLMs cannot directly write control-plane files.
2. All control-plane mutations go through deterministic code.
3. Wrapped artifacts are schema-validated and atomically written.
4. Prompt drift cannot bypass write policy.
5. Prompt contracts and validator contracts are aligned.
6. Denied attempts are visible and test-covered.

## Concrete Todo List

1. Create `hooks/write_policy.py`.
2. Define path classification for task files.
3. Define role allowlists.
4. Add `WriteAttempt` and `WriteDecision`.
5. Add `decide_write()` and `require_write_allowed()`.
6. Add policy matrix tests.
7. Integrate policy checks into `ctl.py`.
8. Integrate policy checks into receipt writers.
9. Freeze direct writes to `manifest.json`.
10. Freeze direct writes to `receipts/**`.
11. Freeze direct writes to `handoff-*.json`.
12. Freeze direct writes to `external-solution-gate.json`.
13. Add `ctl write-execution-graph`.
14. Add `ctl write-repair-log`.
15. Add optional `ctl write-classification`.
16. Move hybrid artifact persistence behind wrappers.
17. Add normalization rules to wrappers.
18. Add write-policy denial logging.
19. Add end-to-end enforcement tests.
20. Add prompt regression scans.
21. Add dashboard/reporting for denied writes.
22. Repeat until no direct LLM control-plane writes remain.

---

## Addendum: Per-Actor Resolution & Permissions-ON Operation (v7.4.1)

Implemented in v7.4.1 per `docs/permissions-on-design.md`. This addendum is normative
for the additions; the sections above remain accurate for the original
artifact classes.

### Actor identity (D3)

- The MAIN session is pinned at SessionStart: `.dynos/orchestrator-session.json`
  (hook-owned; every agent write to it is denied). Tool calls matching the
  pinned `session_id` resolve to the `orchestrator` role and never read role
  files — stamping a subagent role cannot mutate the orchestrator's rights.
- Subagent sessions consume single-use grants from
  `.dynos/task-*/role-grants.json` (wrapper-required: `ctl grant-role` /
  `ctl stamp-role`, allowlist-enforced). The first tool call of an unknown
  session binds it (`role-bindings.json`, hook-owned); the binding is
  immutable for the session's lifetime. `ctl clear-role` expires unconsumed
  grants and is the only sanctioned cleanup (privilege-reducing).
- Sessions without a pin fall back to the legacy `active-segment-role`
  chain — no regression for pre-pin sessions.
- Degraded mode: if the harness ever fails to give subagents distinct
  session ids, their calls resolve as `orchestrator` and fail CLOSED with a
  denial that names the condition ("degraded actor resolution").

### Orchestrator role surface

Allowed: `_scratch/**`, `execution-log.md`, `escalation.md`,
`audit-context.md`, `raw-input.md`, `discovery-notes.md`,
`design-decisions.md`; repo files ONLY while `manifest.stage == EXECUTION`
with `fast_track == true` (inline execution — capability follows the ctl
state machine). Everything else (spec/plan/audit-reports/evidence/
control-plane) is denied with a self-explaining message.

### New namespaces and sinks

- `.dynos/task-*/_scratch/**` — sanctioned temp space for all recognized
  actor roles. Proof-irrelevant by construction: no gate, receipt, or
  validator reads it (CI-asserted by tests/test_scratch_namespace.py).
- `/dev/null`, `/dev/stdout`, `/dev/stderr`, `/dev/tty` — always-allowed
  sinks (not persistence).
- `evidence/verification/**` — ctl-captured verification records (D6);
  agent roles, including executors, are denied. Written only by
  `ctl run-verification-evidence`, hash-bound by a receipt, consumed by
  the spec-completion auditor as machine evidence for execution-based
  acceptance criteria.

### Self-modification guard (H1)

Agent roles are denied writes under the installed plugin root,
`~/.claude/plugins/`, `~/.claude/settings*.json`, and `~/.dynos/` — the
"agent edits its own guardrails" primitive. Developer mode (the project IS
the plugin source checkout) exempts the plugin-root check only.

### Honest enforcement statement

The Bash pre-filter (now shlex-based and quote-aware) is defense-in-depth,
NOT the trust anchor: interpreter-internal writes (`python3 -c
"open(p,'w')"`) are invisible to any command-text parser. The unforgeable
guarantees are: capability-keyed `require_write_allowed` inside ctl,
receipts + the task receipt chain, and the hook-owned `spawn-log.jsonl`
cross-check at audit-receipt time.

### Permissions-ON operation

All skill-prescribed deterministic steps run through one funnel:
`<plugin-root>/bin/dynos` (`dynos ctl ...`, `dynos hook ...`). JSON payloads
travel over stdin (`--from -` + heredoc) — nothing is staged at /tmp or any
raw path. A permissions-ON user can allow the single `bin/dynos` prefix and
then interacts only with the human gates (discovery questions, SPEC_REVIEW,
PLAN_REVIEW, TDD_REVIEW). `tests/test_skill_prose_policy_contract.py` keeps
prose and policy from drifting apart again.
