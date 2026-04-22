# dynos-work System Workflow Trace

This document traces the **live code in `/Users/hassam/Documents/dynos-work`** end to end.

It is intentionally code-first:
- every major step names the real file(s)
- every major step names the real function(s)
- key functions are copied into this document as **verbatim source excerpts**
- each step explains what the code is doing and where the trust boundary is

Important scope note:
- this traces the **current live branch in `dynos-work`**
- it does **not** trace the newer experimental/cost worktree unless explicitly stated
- in this live branch, some sequencing is already deterministic, but **a lot of the pipeline is still skill/prompt-driven**

## 1. Top-Level Shape

The system is split across a small number of orchestration layers:

1. **Skill markdown** in [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md), [skills/plan/SKILL.md](/Users/hassam/Documents/dynos-work/skills/plan/SKILL.md), [skills/execute/SKILL.md](/Users/hassam/Documents/dynos-work/skills/execute/SKILL.md), and [skills/audit/SKILL.md](/Users/hassam/Documents/dynos-work/skills/audit/SKILL.md)
2. **Deterministic CLI and state machine helpers** in [hooks/ctl.py](/Users/hassam/Documents/dynos-work/hooks/ctl.py) and [hooks/lib_core.py](/Users/hassam/Documents/dynos-work/hooks/lib_core.py)
3. **Routing / learned-agent injection** in [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py)
4. **Artifact validation and retrospective scoring** in [hooks/lib_validate.py](/Users/hassam/Documents/dynos-work/hooks/lib_validate.py)
5. **Receipt writers / proof objects** in [hooks/lib_receipts.py](/Users/hassam/Documents/dynos-work/hooks/lib_receipts.py)
6. **Receipt-driven scheduler** in [hooks/scheduler.py](/Users/hassam/Documents/dynos-work/hooks/scheduler.py)
7. **Post-DONE calibration / event handling** in [hooks/eventbus.py](/Users/hassam/Documents/dynos-work/hooks/eventbus.py)
8. **Repair learning loop** in [memory/lib_qlearn.py](/Users/hassam/Documents/dynos-work/memory/lib_qlearn.py)

## 2. Canonical Stage Machine

The core state graph lives in [hooks/lib_core.py](/Users/hassam/Documents/dynos-work/hooks/lib_core.py:76).

### Source excerpt: `ALLOWED_STAGE_TRANSITIONS` and `NEXT_COMMAND`

```python
ALLOWED_STAGE_TRANSITIONS: dict[str, set[str]] = {
    "CLASSIFY_AND_SPEC": {"SPEC_NORMALIZATION", "SPEC_REVIEW", "PLANNING", "FAILED", "CANCELLED"},
    "FOUNDRY_INITIALIZED": {"SPEC_NORMALIZATION", "FAILED"},
    "SPEC_NORMALIZATION": {"SPEC_REVIEW", "FAILED"},
    "SPEC_REVIEW": {"SPEC_NORMALIZATION", "PLANNING", "FAILED"},
    "PLANNING": {"PLAN_REVIEW", "FAILED"},
    "PLAN_REVIEW": {"PLANNING", "PLAN_AUDIT", "FAILED"},
    "PLAN_AUDIT": {"TDD_REVIEW", "PRE_EXECUTION_SNAPSHOT", "FAILED", "PLANNING"},
    "TDD_REVIEW": {"PRE_EXECUTION_SNAPSHOT", "FAILED"},
    "PRE_EXECUTION_SNAPSHOT": {"EXECUTION", "FAILED"},
    "EXECUTION": {"TEST_EXECUTION", "REPAIR_PLANNING", "FAILED"},
    "TEST_EXECUTION": {"CHECKPOINT_AUDIT", "REPAIR_PLANNING", "FAILED"},
    "CHECKPOINT_AUDIT": {"REPAIR_PLANNING", "DONE", "FAILED"},
    "REPAIR_PLANNING": {"REPAIR_EXECUTION", "FAILED"},
    "REPAIR_EXECUTION": {"CHECKPOINT_AUDIT", "REPAIR_PLANNING", "FAILED"},
    "FINAL_AUDIT": {"CHECKPOINT_AUDIT", "DONE", "FAILED"},
    "EXECUTION_GRAPH_BUILD": {"PRE_EXECUTION_SNAPSHOT", "EXECUTION", "FAILED"},
    "DONE": {"CALIBRATED", "FAILED"},
    "CALIBRATED": set(),
    "CANCELLED": set(),
    "FAILED": set(),
}

NEXT_COMMAND: dict[str, str] = {
    "CLASSIFY_AND_SPEC": "/dynos-work:start",
    "FOUNDRY_INITIALIZED": "/dynos-work:start",
    "SPEC_NORMALIZATION": "/dynos-work:start",
    "SPEC_REVIEW": "/dynos-work:start",
    "PLANNING": "/dynos-work:plan",
    "PLAN_REVIEW": "/dynos-work:plan",
    "PLAN_AUDIT": "/dynos-work:plan",
    "TDD_REVIEW": "/dynos-work:plan",
    "EXECUTION_GRAPH_BUILD": "/dynos-work:execute",
    "PRE_EXECUTION_SNAPSHOT": "/dynos-work:execute",
    "EXECUTION": "/dynos-work:execute",
    "TEST_EXECUTION": "/dynos-work:execute",
    "CHECKPOINT_AUDIT": "/dynos-work:audit",
    "REPAIR_PLANNING": "/dynos-work:audit",
    "REPAIR_EXECUTION": "/dynos-work:audit",
    "FINAL_AUDIT": "/dynos-work:audit",
    "DONE": "Task complete",
    "CALIBRATED": "Task calibrated",
    "CANCELLED": "Task cancelled",
    "FAILED": "Review failure state before continuing",
}
```

### Explanation

- `ALLOWED_STAGE_TRANSITIONS` is the hard graph.
- `NEXT_COMMAND` is the skill handoff lookup used to tell the operator which skill should run next.
- This means the repo has two levels of orchestration:
  - hard legality in code
  - softer “what should run next” guidance in the skill layer

## 3. Core Transition Function

The real stage transition engine is [transition_task()](/Users/hassam/Documents/dynos-work/hooks/lib_core.py:1125).

### Source excerpt: `transition_task(...)` opening

```python
def transition_task(
    task_dir: Path,
    next_stage: str,
    *,
    force: bool = False,
    force_reason: str | None = None,
    force_approver: str | None = None,
) -> tuple[str, dict]:
    """Transition a task to a new stage, enforcing allowed transitions.
    ...
    """
    if force:
        if not isinstance(force_reason, str) or not force_reason.strip():
            raise ValueError(
                "force_reason must be a non-empty string when force=True "
                "(whitespace-only values are rejected — whitespace carries no "
                "human-readable justification)"
            )
        if not isinstance(force_approver, str) or not force_approver.strip():
            raise ValueError(
                "force_approver must be a non-empty string when force=True "
                "(whitespace-only values are rejected)"
            )

    manifest_path = task_dir / "manifest.json"
    manifest = load_json(manifest_path)
    ...
    current_stage = manifest.get("stage")
    if next_stage not in ALLOWED_STAGE_TRANSITIONS:
        raise ValueError(f"Unknown stage: {next_stage}")
    if not force and next_stage not in ALLOWED_STAGE_TRANSITIONS.get(current_stage, set()):
        raise ValueError(f"Illegal stage transition: {current_stage} -> {next_stage}")
```

### Source excerpt: important gate clauses inside `transition_task(...)`

```python
if current_stage == "SPEC_REVIEW" and next_stage == "PLANNING":
    _check_human_approval("SPEC_REVIEW", task_dir / "spec.md")

if current_stage == "PLAN_REVIEW" and next_stage == "PLAN_AUDIT":
    _check_human_approval("PLAN_REVIEW", task_dir / "plan.md")

if current_stage == "TDD_REVIEW" and next_stage == "PRE_EXECUTION_SNAPSHOT":
    _check_human_approval("TDD_REVIEW", task_dir / "evidence" / "tdd-tests.md")

if current_stage == "PRE_EXECUTION_SNAPSHOT" and next_stage == "EXECUTION":
    _check_tdd_tests(task_dir, current_stage, next_stage)

if next_stage == "EXECUTION":
    match_result = plan_validated_receipt_matches(task_dir)
    if match_result is True:
        pass
    elif isinstance(match_result, str):
        gate_errors.append(f"plan-validated: {match_result}")
    else:
        gate_errors.append(
            "receipt: plan-validated (plan was never validated)"
        )

if next_stage == "CHECKPOINT_AUDIT":
    routing_payload = read_receipt(task_dir, "executor-routing")
    ...

if next_stage == "DONE":
    if not (task_dir / "task-retrospective.json").exists():
        gate_errors.append("task-retrospective.json (run /dynos-work:audit to generate)")
    if not list(task_dir.glob("audit-reports/*.json")):
        gate_errors.append("audit-reports/ (no audit reports found — audit was never run)")
    if read_receipt(task_dir, "retrospective") is None:
        gate_errors.append("receipt: retrospective (reward was never computed via receipts)")
```

### Explanation

`transition_task()` is the **real choke point**. Even when a skill tells the model to run something, the transition still has to survive these checks:

- review approvals are hash-bound to the current artifact
- TDD review is hash-bound to `evidence/tdd-tests.md`
- execution requires a fresh `plan-validated` receipt
- checkpoint audit requires executor routing plus per-segment receipts
- `DONE` requires audit artifacts, retrospective, and receipt chain integrity

This is the main anti-bypass layer in the live system.

## 4. Deterministic CLI Entry Points

The main CLI entry points live in [hooks/ctl.py](/Users/hassam/Documents/dynos-work/hooks/ctl.py).

### 4.1 `validate-task`

Source: [cmd_validate_task()](/Users/hassam/Documents/dynos-work/hooks/ctl.py:79)

```python
def cmd_validate_task(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    root = _root_for_task_dir(task_dir)
    blocked = _refuse_if_rules_corrupt(root)
    if blocked is not None:
        return blocked
    errors = validate_task_artifacts(task_dir, strict=args.strict)
    if errors:
        print("Validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Validation passed.")
    return 0
```

Explanation:
- this is the deterministic artifact validator
- it refuses task creation if prevention rules are corrupt
- then it delegates to `validate_task_artifacts()`

### 4.2 `transition`

Source: [cmd_transition()](/Users/hassam/Documents/dynos-work/hooks/ctl.py:98)

```python
def cmd_transition(args: argparse.Namespace) -> int:
    task_dir = Path(args.task_dir).resolve()
    ...
    try:
        previous, manifest = transition_task(
            task_dir,
            args.next_stage,
            force=args.force,
            force_reason=force_reason,
            force_approver=force_approver,
        )
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{manifest['task_id']}: {previous} -> {manifest['stage']}")
    return 0
```

Explanation:
- thin CLI wrapper
- almost no workflow logic lives here
- the real behavior is in `transition_task()`

### 4.3 `approve-stage`

Source: [cmd_approve_stage()](/Users/hassam/Documents/dynos-work/hooks/ctl.py:140)

```python
def cmd_approve_stage(args: argparse.Namespace) -> int:
    """Record a human approval receipt for a review stage.
    ...
    """
    stage = args.stage
    mapping = _APPROVE_STAGE_MAP.get(stage)
    ...
    artifact_rel, _ = mapping

    task_dir = Path(args.task_dir).resolve()
    artifact_path = task_dir / artifact_rel
    ...
    sha256_hex = hash_file(artifact_path)
    ...
    receipt_human_approval(task_dir, stage, sha256_hex, approver="human")
    ...
    print(f"{task_dir.name}: approved {stage} ({sha256_hex[:12]}) — receipt written, scheduler will advance")
    return 0
```

Explanation:
- this writes the human-approval receipt
- it **does not** directly advance the stage
- instead the **scheduler** notices the receipt and advances if proofs are sufficient

## 5. Receipt-Driven Scheduler

The deterministic receipt scheduler is in [hooks/scheduler.py](/Users/hassam/Documents/dynos-work/hooks/scheduler.py).

### Source excerpt: `compute_next_stage(task_dir)`

```python
def compute_next_stage(task_dir: Path) -> tuple[str | None, list[str]]:
    ...
    manifest_path = task_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    current_stage = manifest.get("stage") if isinstance(manifest, dict) else None

    if current_stage != "SPEC_REVIEW":
        return (None, [])

    receipt_step = "human-approval-SPEC_REVIEW"
    receipt_path = _receipts_dir(task_dir) / f"{receipt_step}.json"
    artifact_path = task_dir / "spec.md"
    next_stage = "PLANNING"

    receipt = read_receipt(task_dir, receipt_step)
    if receipt is None:
        return (
            next_stage,
            [f"missing {receipt_step} at {receipt_path}"],
        )

    if not artifact_path.exists():
        return (
            next_stage,
            [
                f"receipt {receipt_step} present at {receipt_path} but "
                f"artifact missing at {artifact_path}"
            ],
        )

    expected = receipt.get("artifact_sha256") or ""
    actual = hash_file(artifact_path)
    if not isinstance(expected, str) or expected != actual:
        return (
            next_stage,
            [
                f"hash mismatch for {receipt_step}: "
                f"expected={(expected or '')[:12]} actual={actual[:12]}"
            ],
        )

    return (next_stage, [])
```

### Source excerpt: `handle_receipt_written(...)`

```python
def handle_receipt_written(
    task_dir: Path,
    receipt_step: str,
    receipt_sha256: str,
) -> None:
    if receipt_step == _SCHEDULER_REFUSED_STEP:
        return

    del receipt_sha256

    try:
        result = compute_next_stage(task_dir)
        next_stage, missing_proofs = result

        if next_stage is None:
            return

        current_stage = _read_current_stage(task_dir)
        task_id = _read_task_id(task_dir)
        root = task_dir.parent.parent

        if not missing_proofs:
            try:
                transition_task(task_dir, next_stage)
            except ValueError as race_exc:
                ...
            return

        log_event(
            root,
            "scheduler_transition_refused",
            task=task_id,
            task_id=task_id,
            current_stage=current_stage,
            proposed_stage=next_stage,
            missing_proofs=list(missing_proofs),
        )
        ...
```

### Explanation

In the live branch, scheduler coverage is **narrow**:
- it currently handles only `SPEC_REVIEW -> PLANNING`
- everything else is still mostly driven by the skills calling ctl / receipt writers

This matters because it tells you exactly where orchestration is hard-coded vs still prompt-led.

## 6. Start Skill Flow

The main user-facing entrypoint is [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md).

The high-level phases in the live skill are:

1. Intake
2. Discovery + Design + Classification
3. Spec Normalization
4. Spec Review
5. Generate Plan + Execution Graph
6. Plan Review
7. Plan Audit
8. TDD-First Gate
9. Handoff

### What actually happens

The start skill is doing a lot of orchestration in prose:
- it tells the planner what to do
- it tells the operator/model when to write `discovery-notes.md`
- it tells the operator/model when to write `design-decisions.md`
- it tells the operator/model when to call receipts and transitions

The code-enforced parts of that start flow are:
- planner prompt sidecars
- planner spawn receipts
- artifact validation
- transition gates in `transition_task()`

### Source excerpt: planner prompt sidecar writer

Source: [cmd_planner_inject_prompt()](/Users/hassam/Documents/dynos-work/hooks/router.py:1618)

```python
def cmd_planner_inject_prompt(args: argparse.Namespace) -> int:
    """Write a per-phase injected-prompt sidecar for a planner spawn.
    ...
    """
    import sys as _sys
    import re as _re
    if not _re.match(r"^task-[A-Za-z0-9][A-Za-z0-9_.-]*$", args.task_id):
        print(
            json.dumps({"error": f"invalid task-id (must match ^task-[A-Za-z0-9][A-Za-z0-9_.-]*$): {args.task_id!r}"}),
            file=_sys.stderr,
        )
        return 1

    stdin_bytes = _sys.stdin.buffer.read()
    root = Path(args.root).resolve()
    sidecar_dir = (
        root / ".dynos" / args.task_id / "receipts"
        / INJECTED_PLANNER_PROMPTS_DIR
    ).resolve()
    ...
```

### Source excerpt: planner receipt writer

Source: [receipt_planner_spawn()](/Users/hassam/Documents/dynos-work/hooks/lib_receipts.py:1380)

```python
def receipt_planner_spawn(
    task_dir: Path,
    phase: str,
    tokens_used: int,
    model_used: str | None = None,
    agent_name: str | None = None,
    injected_prompt_sha256: str = _INJECTED_PROMPT_SHA256_MISSING,
) -> Path:
    ...
    if injected_prompt_sha256 is _INJECTED_PROMPT_SHA256_MISSING:
        raise TypeError(
            "receipt_planner_spawn: injected_prompt_sha256 is required. "
            "Pass a non-empty sha256 hex digest obtained from "
            "`hooks/router.py planner-inject-prompt --task-id <id> "
            "--phase <phase>`."
        )
    ...
    sidecar_file = (
        task_dir / "receipts" / INJECTED_PLANNER_PROMPTS_DIR
        / f"{phase}.sha256"
    )
    if not sidecar_file.exists():
        raise ValueError(
            f"receipt_planner_spawn: planner sidecar missing for phase "
            f"{phase!r} at {sidecar_file}."
        )
    ...
```

### Explanation

This is one of the more important integrity checks in the system:
- the planner prompt must be materialized into a sidecar
- the receipt writer refuses if the sidecar is missing
- this makes planner prompt injection auditable after the fact

## 7. Artifact Validation

The core artifact validator is [validate_task_artifacts()](/Users/hassam/Documents/dynos-work/hooks/lib_validate.py:240).

### Source excerpt

```python
def validate_task_artifacts(
    task_dir: Path,
    strict: bool = False,
    *,
    run_gap: bool = True,
) -> list[str]:
    ...
    manifest = load_json(manifest_path)
    errors.extend(validate_manifest(manifest))
    stage = manifest.get("stage", "")
    ...
    spec_text = require(spec_path)
    spec_headings = collect_headings(spec_text)
    for heading in REQUIRED_SPEC_HEADINGS:
        if heading not in spec_headings:
            errors.append(f"spec missing heading: {heading}")
    ...
    criteria_numbers = parse_acceptance_criteria(spec_text)
    ...
    if plan_path.exists():
        plan_text = require(plan_path)
        ...
        for heading in all_required:
            if heading not in plan_headings:
                errors.append(f"plan missing heading: {heading}")
        ...
        if run_gap:
            from plan_gap_analysis import findings_from_report, run_gap_analysis
            project_root = task_dir.parent.parent
            gap_report = run_gap_analysis(project_root, task_dir)
            errors.extend(findings_from_report(gap_report))
    ...
    if graph_path.exists():
        graph = load_json(graph_path)
        segments = graph.get("segments")
        ...
        if detect_cycle(graph):
            errors.append("execution graph must be acyclic")
        ...
    errors.extend(validate_repair_log(task_dir))
    errors.extend(validate_retrospective(task_dir))
    return errors
```

### Explanation

This function is the main **static artifact contract** checker:
- spec headings
- acceptance criteria numbering
- plan headings
- reference-code existence
- gap analysis
- execution graph structure
- repair log structure
- retrospective structure

When a skill says “the plan is ready,” this function is the code that proves whether that statement is true.

## 8. Routing and Learned-Agent Selection

The learned-agent router lives in [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py).

### Source excerpt: `resolve_route(...)`

```python
def resolve_route(root: Path, role: str, task_type: str, ctx: RouterContext | None = None) -> dict:
    """Determine whether to use generic, learned, or alongside agent."""
    if not (ctx.learning_enabled if ctx else is_learning_enabled(root)):
        result = {
            "mode": "generic",
            "agent_path": None,
            "agent_name": None,
            "composite_score": 0.0,
            "source": "learning_enabled=false",
        }
        ...
        return result

    registry = ctx.registry if ctx else _read_learned_registry(root)
    agents = registry.get("agents", [])
    ...
    if not learned:
        result = {
            "mode": "generic",
            "agent_path": None,
            "agent_name": None,
            "composite_score": 0.0,
            "source": "no learned agent",
        }
        ...
        return result
    ...
    if role == "security-auditor" and mode == "replace":
        mode = "alongside"
    ...
    result = {
        "mode": mode,
        "agent_path": agent_path,
        "agent_name": agent_name,
        "composite_score": composite,
        "source": f"learned:{agent_name}",
    }
    ...
    return result
```

### Explanation

This is the main learned-agent routing decision:
- if learning is off, route generic
- if no learned agent exists, route generic
- if a learned agent exists, use its mode
- `security-auditor` is protected from full replacement

This function is used by planner/executor/auditor routing paths.

## 9. Executor Prompt Injection

### Source excerpt: `cmd_inject_prompt(...)`

Source: [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py:1351)

```python
def cmd_inject_prompt(args: argparse.Namespace) -> int:
    """Read base prompt from stdin, inject learned agent rules, print result."""
    root = Path(args.root).resolve()
    graph_path = Path(args.graph)
    if not graph_path.exists():
        print(json.dumps({"error": f"graph not found: {graph_path}"}))
        return 1
    graph = load_json(graph_path)
    segments = graph.get("segments", [])
    ...
    if plan_entry is None:
        plan = build_executor_plan(
            root,
            args.task_type,
            [target_seg],
            include_enforced=getattr(args, "include_enforced", False),
        )
        ...
    base_prompt = _sys.stdin.read()
    result = build_executor_prompt(root, target_seg, plan_entry, base_prompt)
    printed_bytes = (result + "\n").encode("utf-8")
    ...
    digest = _write_prompt_sidecar(sidecar_dir, args.segment_id, printed_bytes)
    ...
    _sys.stdout.write(result + "\n")
    _sys.stdout.flush()
    return 0
```

### Explanation

This is how executor prompts become auditable:
- start from a base prompt
- resolve routing
- inject learned-agent content and prevention rules
- write a sidecar for the exact emitted bytes
- print the final prompt for the executor spawn

The live `execute` skill depends heavily on this.

## 10. Audit Planning and Auditor Injection

### Source excerpt: `build_audit_plan(...)`

Source: [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py:805)

```python
def build_audit_plan(
    root: Path,
    task_type: str,
    domains: list[str],
    fast_track: bool = False,
    *,
    ctx: RouterContext | None = None,
) -> dict:
    ...
    registry = _load_auditor_registry(root)
    ...
    if fast_track:
        eligible = list(registry.get("fast_track", _DEFAULT_AUDITOR_REGISTRY["fast_track"]))
    else:
        eligible = list(registry.get("always", _DEFAULT_AUDITOR_REGISTRY["always"]))
        domain_map = registry.get("domain_conditional", _DEFAULT_AUDITOR_REGISTRY["domain_conditional"])
        for domain in domains:
            for auditor in domain_map.get(domain, []):
                if auditor not in eligible:
                    eligible.append(auditor)

    for auditor in eligible:
        skip_decision = resolve_skip(root, auditor, task_type, ctx=ctx)
        if skip_decision["skip"]:
            plan["auditors"].append({
                "name": auditor,
                "action": "skip",
                "reason": skip_decision["reason"],
                "streak": skip_decision["streak"],
                "threshold": skip_decision["threshold"],
            })
            continue

        model_decision = resolve_model(root, auditor, task_type, ctx=ctx)
        ...
```

### Source excerpt: `cmd_audit_inject_prompt(...)`

Source: [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py:1482)

```python
def cmd_audit_inject_prompt(args: argparse.Namespace) -> int:
    """Inject learned-auditor instructions into a base auditor prompt."""
    root = Path(args.root).resolve()
    plan_path = Path(args.audit_plan)
    if not plan_path.exists():
        print(json.dumps({"error": f"audit plan not found: {plan_path}"}))
        return 1
    ...
    target = entry matching args.auditor_name
    ...
    base_prompt = _sys.stdin.read()
    route_mode = target.get("route_mode", "generic")
    agent_path = target.get("agent_path")
    final_text = base_prompt

    if route_mode in ("replace", "alongside") and agent_path:
        ...
        final_text = (
            base_prompt.rstrip()
            + "\n\n## Learned Auditor Instructions\n"
            + f"You are running as the **{auditor_name}** learned auditor "
            + f"(mode={route_mode}). ..."
            + agent_content
        )
    ...
```

### Explanation

This is the audit-side equivalent of executor injection:
- router decides which auditors are eligible
- skip policy may remove some
- model policy chooses the model
- learned auditor content can be injected into the final prompt
- a sidecar is written for the emitted prompt

## 11. Repair Loop and Q-Learning

The live repair loop is less deterministic than the newer cost worktree. In this branch, the ctl wrappers are thin and the real policy lives in `memory/lib_qlearn.py`.

### Source excerpt: `cmd_repair_plan(...)` and `cmd_repair_update(...)`

Source: [hooks/ctl.py](/Users/hassam/Documents/dynos-work/hooks/ctl.py:244)

```python
def cmd_repair_plan(args: argparse.Namespace) -> int:
    import json as _json
    import sys as _sys
    from lib_qlearn import build_repair_plan
    findings = _json.load(_sys.stdin)
    if isinstance(findings, dict):
        findings = findings.get("findings", [])
    result = build_repair_plan(Path(args.root).resolve(), findings, args.task_type)
    print(_json.dumps(result, indent=2))
    return 0

def cmd_repair_update(args: argparse.Namespace) -> int:
    import json as _json
    import sys as _sys
    from lib_qlearn import update_from_outcomes
    data = _json.load(_sys.stdin)
    outcomes = data.get("outcomes", []) if isinstance(data, dict) else data
    result = update_from_outcomes(Path(args.root).resolve(), outcomes, args.task_type)
    print(_json.dumps(result, indent=2))
    return 0
```

### Source excerpt: `build_repair_plan(...)`

Source: [memory/lib_qlearn.py](/Users/hassam/Documents/dynos-work/memory/lib_qlearn.py:288)

```python
def build_repair_plan(
    root: Path,
    findings: list[dict],
    task_type: str,
) -> dict:
    """Build executor, route-mode, and model assignments using hierarchical Q-learning."""
    policy = project_policy(root)
    enabled = bool(policy.get("repair_qlearning", True))
    epsilon = float(policy.get("repair_epsilon", DEFAULT_EPSILON))

    executor_q = load_q_table(root, "executor") if enabled else {"entries": {}}
    route_q = load_q_table(root, "route") if enabled else {"entries": {}}
    model_q = load_q_table(root, "model") if enabled else {"entries": {}}

    assignments = []
    for finding in findings:
        finding_id = finding.get("id", finding.get("finding_id", "unknown"))
        severity = finding.get("severity", "medium")
        auditor = finding.get("auditor", finding.get("auditor_name", ""))
        retry_count = int(finding.get("retry_count", 0))
        category = _finding_category(finding_id)

        base_state = encode_repair_state(category, severity, task_type, retry_count)
        valid_executors = _executors_for_category(category, root)
        ...
        if retry_count >= ESCALATION_RETRY_THRESHOLD:
            model = "opus"
            model_source = "escalation"
        elif auditor.startswith("security"):
            model = "opus"
            model_source = "security_floor"
        ...

        assignments.append({
            "finding_id": finding_id,
            "state": base_state,
            "assigned_executor": executor,
            "executor_source": executor_source,
            "route_mode": route_mode,
            "route_source": route_source,
            "agent_path": agent_path if route_mode == "learned" else None,
            "agent_name": agent_name if route_mode == "learned" else None,
            "model_override": model,
            "model_source": model_source,
        })

    return {
        "generated_at": now_iso(),
        "source": "q-learning" if enabled else "default",
        "enabled": enabled,
        "epsilon": epsilon if enabled else None,
        "assignments": assignments,
    }
```

### Explanation

The live branch repair loop works like this:

1. auditors produce findings
2. audit skill (plus prose logic) decides what findings to feed into repair planning
3. `ctl repair-plan` delegates to `build_repair_plan()`
4. that function chooses:
   - executor
   - route mode
   - model
5. later, outcomes are fed back through `update_from_outcomes()`

This is a real control path, but in the live branch it is **not yet wrapped into a fully deterministic repair-log compiler** the way the newer worktree is.

## 12. Retrospective and Reward Computation

### Source excerpt: `cmd_compute_reward(...)`

Source: [hooks/ctl.py](/Users/hassam/Documents/dynos-work/hooks/ctl.py:267)

```python
def cmd_compute_reward(args: argparse.Namespace) -> int:
    import json
    from lib_validate import compute_reward
    task_dir = Path(args.task_dir).resolve()
    result = compute_reward(task_dir)
    if args.write:
        from lib_core import write_json
        write_json(task_dir / "task-retrospective.json", result)
        print(f"Written to {task_dir / 'task-retrospective.json'}")
        try:
            from lib_receipts import receipt_retrospective
            receipt_retrospective(task_dir)
        except Exception as exc:
            print(f"[warn] retrospective receipt failed: {exc}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2))
    return 0
```

### Source excerpt: `compute_reward(task_dir)`

Source: [hooks/lib_validate.py](/Users/hassam/Documents/dynos-work/hooks/lib_validate.py:526)

```python
def compute_reward(task_dir: Path) -> dict:
    """Deterministically compute reward scores from task artifacts."""
    task_dir = Path(task_dir)
    task_id = task_dir.name
    root = task_dir.parent.parent
    ...
    routing_receipt = read_receipt(task_dir, "audit-routing")
    if routing_receipt is None:
        valid_auditor_names = None
        log_event(
            root,
            "auditor_cross_check_skipped",
            task=task_id,
            reason="audit-routing receipt missing",
        )
        _auditor_check_disabled = True
    else:
        valid_auditor_names = {
            a["name"]
            for a in routing_receipt.get("auditors", [])
            if isinstance(a, dict)
            and a.get("action") == "spawn"
            and isinstance(a.get("name"), str)
            and a.get("name")
        }
        _auditor_check_disabled = False
    ...
    findings_by_auditor: dict[str, int] = {}
    findings_by_category: dict[str, int] = {}
    total_findings = 0
    total_blocking = 0
    ...
    repair_log_path = task_dir / "repair-log.json"
    if repair_log_path.exists():
        ...
    ...
    manifest = load_json(task_dir / "manifest.json")
    classification = manifest.get("classification", {})
    task_type = classification.get("type", "feature")
    task_domains = ",".join(classification.get("domains", []))
    task_risk_level = classification.get("risk_level", "medium")
    ...
```

### Explanation

This function is the **retrospective compiler**:
- reads audit reports
- cross-checks auditors against the routing receipt
- reads repair log
- reads manifest classification
- computes counts and scores
- writes the normalized retrospective structure used by learning/calibration

This is one of the stronger deterministic pieces in the system.

## 13. Completion and Calibration

After `DONE`, calibration work is triggered through the event system.

### Source excerpt: `eventbus.py` DONE -> CALIBRATED path

Source: [hooks/eventbus.py](/Users/hassam/Documents/dynos-work/hooks/eventbus.py:684)

```python
# AC 22(c): transition to CALIBRATED. Idempotency is enforced by
# load_json + ALLOWED_STAGE_TRANSITIONS — if already CALIBRATED,
# transition_task raises and we swallow it.
try:
    from lib_core import transition_task, load_json
    manifest = load_json(task_dir / "manifest.json")
    if manifest.get("stage") == "CALIBRATED":
        continue
    transition_task(task_dir, "CALIBRATED")
except Exception as exc:
    print(
        f"  [warn] CALIBRATED transition failed for {td}: {exc}",
        file=sys.stderr,
    )
```

### Explanation

The lifecycle after `DONE` is:

1. learning/calibration handlers run
2. policy deltas or no-op receipts are written
3. eventbus attempts `DONE -> CALIBRATED`
4. idempotency is enforced by the same `transition_task()` stage machine

## 14. End-to-End Walkthrough

This is the shortest honest end-to-end trace of the live branch:

### Step A: User starts a task

Files involved:
- [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md)
- `.dynos/task-<id>/manifest.json`
- `.dynos/task-<id>/raw-input.md`

What happens:
- the start skill scaffolds task artifacts
- planner prompts are injected and recorded with sidecars
- planner receipts are written after planner spawns

Trust boundary:
- skill prose still controls a lot of sequencing here
- sidecar and receipt checks make the planner prompt auditable

### Step B: Spec is generated and reviewed

Files involved:
- `spec.md`
- `receipts/human-approval-SPEC_REVIEW.json`
- [hooks/scheduler.py](/Users/hassam/Documents/dynos-work/hooks/scheduler.py)

What happens:
- spec gets written
- human approval receipt is written through `approve-stage`
- scheduler notices the receipt and may advance to `PLANNING`

Trust boundary:
- approval is deterministic and hash-bound
- advancement for this edge is scheduler-driven

### Step C: Plan and execution graph are produced

Files involved:
- `plan.md`
- `execution-graph.json`
- [hooks/lib_validate.py](/Users/hassam/Documents/dynos-work/hooks/lib_validate.py)

What happens:
- planner writes plan and graph
- `validate_task_artifacts()` checks them
- `plan-validated` receipt becomes the proof object used later by execution gates

Trust boundary:
- planner content is LLM-authored
- shape/integrity of plan and graph is validated deterministically

### Step D: Execution routes work to executors

Files involved:
- [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py)
- `receipts/_injected-prompts/*.sha256`
- executor receipts

What happens:
- router picks generic/learned mode
- executor prompt is built and sidecar-hashed
- executors run
- ownership checks and executor receipts prove segment completion

Trust boundary:
- actual code edits are LLM-authored
- prompt injection, routing, and segment proof chain are code-enforced

### Step E: Audit runs

Files involved:
- `audit-plan.json`
- `audit-reports/*.json`
- `receipts/audit-routing.json`
- `receipts/audit-*.json`

What happens:
- audit plan is built deterministically
- auditors are skipped/spawned according to registry and skip policy
- learned auditor prompts may be injected
- receipts capture the resulting reports

Trust boundary:
- auditors still generate findings via LLM prompts
- routing and receipt validation are deterministic

### Step F: Repair loop runs

Files involved:
- `repair-log.json`
- [memory/lib_qlearn.py](/Users/hassam/Documents/dynos-work/memory/lib_qlearn.py)

What happens:
- audit findings are fed into Q-learning repair planning
- executors fix findings
- outcomes are fed back into the Q-tables

Trust boundary:
- this live branch still leaves more of the repair choreography in prompt logic than the newer worktree

### Step G: Retrospective and calibration

Files involved:
- `task-retrospective.json`
- calibration receipts
- [hooks/eventbus.py](/Users/hassam/Documents/dynos-work/hooks/eventbus.py)

What happens:
- `compute_reward()` compiles retrospective scores from artifacts
- learning/calibration handlers update persistent policy
- eventbus transitions `DONE -> CALIBRATED`

Trust boundary:
- retrospective scoring is deterministic
- calibration is event-driven after completion

## 15. Where the Live Branch Is Deterministic vs Prompt-Driven

### Mostly deterministic

- stage legality
- review approval receipts
- hash-bound artifact freshness
- planner/executor/auditor sidecar proof
- artifact validation
- retrospective computation
- `DONE -> CALIBRATED` lifecycle edge

### Still significantly prompt-driven

- start-skill sequencing
- discovery/design/classification flow
- large parts of execution orchestration
- large parts of audit orchestration
- repair-log construction and repair batching in the live branch

## 16. Fastest Files To Read If You Want To Reconstruct The System Yourself

Read these in this order:

1. [hooks/lib_core.py](/Users/hassam/Documents/dynos-work/hooks/lib_core.py)
2. [hooks/ctl.py](/Users/hassam/Documents/dynos-work/hooks/ctl.py)
3. [hooks/router.py](/Users/hassam/Documents/dynos-work/hooks/router.py)
4. [hooks/lib_validate.py](/Users/hassam/Documents/dynos-work/hooks/lib_validate.py)
5. [hooks/lib_receipts.py](/Users/hassam/Documents/dynos-work/hooks/lib_receipts.py)
6. [hooks/scheduler.py](/Users/hassam/Documents/dynos-work/hooks/scheduler.py)
7. [hooks/eventbus.py](/Users/hassam/Documents/dynos-work/hooks/eventbus.py)
8. [memory/lib_qlearn.py](/Users/hassam/Documents/dynos-work/memory/lib_qlearn.py)
9. [skills/start/SKILL.md](/Users/hassam/Documents/dynos-work/skills/start/SKILL.md)
10. [skills/execute/SKILL.md](/Users/hassam/Documents/dynos-work/skills/execute/SKILL.md)
11. [skills/audit/SKILL.md](/Users/hassam/Documents/dynos-work/skills/audit/SKILL.md)

## 17. Blunt Summary

The live dynos-work repo is **not** a pure deterministic workflow engine.

What it actually is:
- a deterministic **state machine + receipt/gate substrate**
- a deterministic **routing / validation / retrospective substrate**
- wrapped around a still-large amount of **skill/prompt orchestration**

That is the honest end-to-end trace of the code currently in this branch.
