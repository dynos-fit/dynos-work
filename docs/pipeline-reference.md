# dynos-work Pipeline Reference

## Table of Contents

1. Pipeline Overview
2. Stage Transition Graph
3. Phase 1: Start (Steps 0-9)
4. Phase 2: Execute (Steps 0-5)
5. Phase 3: Audit (Steps 0-5)
6. Phase 4: Post-Completion Event Bus Chain
7. Phase 5: Learn
8. Phase 6: Evolve
9. Receipt Chain
10. Token Recording Flow
11. Learned Agent Injection Flow
12. Router Decision Flow
13. Contract Validation Chain
14. File System Layout

---

## 1. Pipeline Overview

The dynos-work pipeline transforms a user's task description into audited, tested, production code through four sequential skill phases plus an automated post-completion chain. Every transition between phases is gated by receipts, deterministic validation, and human approval.

**Skill invocation order:**
`/dynos-work:start` → `/dynos-work:execute` → `/dynos-work:audit` → (automatic) `task-completed` event bus drain.

**Ownership principle:** Each skill owns specific stages. The `NEXT_COMMAND` map in `dynoslib_core.py` enforces which skill handles which stage.

---

## 2. Stage Transition Graph

18 stages defined. Allowed transitions enforced by `ALLOWED_STAGE_TRANSITIONS` in `dynoslib_core.py`.

**Linear happy path:**

```
FOUNDRY_INITIALIZED → SPEC_NORMALIZATION → SPEC_REVIEW → PLANNING →
PLAN_REVIEW → PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT → EXECUTION →
TEST_EXECUTION → CHECKPOINT_AUDIT → DONE
```

**Repair loop (within audit):**

```
CHECKPOINT_AUDIT → REPAIR_PLANNING → REPAIR_EXECUTION → CHECKPOINT_AUDIT (loop)
```

**Failure paths:** Every non-terminal stage can transition to FAILED. DONE, CANCELLED, and FAILED are terminal.

**Stage-to-skill ownership:**

| Stage Range | Owning Skill | Invocation |
|---|---|---|
| FOUNDRY_INITIALIZED through PRE_EXECUTION_SNAPSHOT | start | `/dynos-work:start` |
| EXECUTION through TEST_EXECUTION | execute | `/dynos-work:execute` |
| CHECKPOINT_AUDIT through REPAIR_EXECUTION, DONE | audit | `/dynos-work:audit` |

---

## 3. Phase 1: Start Skill (Steps 0-9)

### Step 0: Metadata & Initialization

| Item | Detail |
|---|---|
| **Trigger** | User runs `/dynos-work:start` with task description |
| **Input** | User-provided task description (text, PRD, screenshot, URL) |
| **Actions** | 1. Ensure `.dynos/` exists. 2. Register project (`dynoregistry.py`). 3. Start daemon (`dynomaintain.py`). 4. Generate task ID. 5. Create task directory. 6. Write `raw-input.md`. 7. Write `manifest.json`. 8. Init `execution-log.md`. 9. Validate manifest. |
| **Hooks called** | `dynoregistry.py register`, `dynomaintain.py start`, `dynosctl.py validate-task` |
| **Output** | `manifest.json`, `raw-input.md`, `execution-log.md` |
| **Validation** | manifest.json must parse with `task_id`, `created_at`, `raw_input`, `stage` |

### Step 1: Discovery Intake

| Item | Detail |
|---|---|
| **Input** | `raw-input.md`, optional `trajectories.json` |
| **Actions** | Build context, generate up to 5 questions, present to user, write Q&A |
| **Output** | `discovery-notes.md` |

### Step 2: Discovery + Design + Classification

| Item | Detail |
|---|---|
| **Input** | `raw-input.md`, `discovery-notes.md` |
| **Fast-track skip** | If task is well-scoped (specific files, explicit constraints, bounded change): skip planner, classify directly |
| **Normal path** | 1. Check for learned plan-skill via `dynorouter.py resolve_route`. 2. Spawn Planner (with learned agent injected if available). 3. Present questions. 4. Write `design-decisions.md`. 5. Write classification to `manifest.json`. 6. Validate classification. |
| **Receipts** | `plan-routing`, `planner-discovery` |
| **Stage transition** | FOUNDRY_INITIALIZED → SPEC_NORMALIZATION |
| **Output** | `discovery-notes.md`, `design-decisions.md`, classification in manifest |

### Step 2b: Fast-Track Gate

| Item | Detail |
|---|---|
| **Input** | `manifest.json` classification |
| **Check** | `risk_level == "low"` AND exactly 1 domain |
| **Writes** | `fast_track: true/false` to manifest |
| **Effect** | Simplifies spec, single-segment plan, reduced auditors |

### Step 3: Spec Normalization

| Item | Detail |
|---|---|
| **Input** | `raw-input.md`, `discovery-notes.md`, `design-decisions.md`, implementation files |
| **Actions** | Spawn Planner to write `spec.md`. Validate: 7 required headings, numbered criteria, testable criteria, labeled assumptions. |
| **Receipts** | `planner-spec`, `spec-validated` |
| **Stage transition** | SPEC_NORMALIZATION → SPEC_REVIEW |
| **Output** | `spec.md` |

### Step 4: Spec Review (HUMAN GATE)

| Item | Detail |
|---|---|
| **Input** | `spec.md` |
| **Actions** | Present to user. Approved → advance. Changes → re-run Step 3. Rejected → FAILED. |
| **Stage transition** | SPEC_REVIEW → PLANNING |
| **Cannot be skipped** | Hard rule |

### Step 5: Generate Plan + Execution Graph

| Item | Detail |
|---|---|
| **Input** | `spec.md`, `design-decisions.md` |
| **Mode selection** | Hierarchical if high/critical risk or >10 criteria. Standard otherwise. |
| **Actions** | Spawn Planner(s). Write `plan.md` + `execution-graph.json`. Run `validate_task_artifacts.py`. |
| **Validation (plan.md)** | 8 required headings, file paths exist, components list files |
| **Validation (graph)** | Valid JSON, unique IDs, valid executors, no file overlap, acyclic deps, full criteria coverage |
| **Receipts** | `planner-plan`, `plan-validated` |
| **Stage transition** | PLANNING → PLAN_REVIEW |

### Step 6: Plan Review (HUMAN GATE)

| Item | Detail |
|---|---|
| **Input** | `plan.md` |
| **Cannot be skipped** | Hard rule |
| **Stage transition** | PLAN_REVIEW → PLAN_AUDIT |

### Step 7: Plan Audit

| Item | Detail |
|---|---|
| **Fast-track skip** | If `fast_track: true` AND `policy.json` has `fast_track_skip_plan_audit: true` |
| **Normal** | Spawn spec-completion-auditor. If gaps: back to planning, repair, re-validate. Create git snapshot branch. |
| **Receipts** | `plan-audit-check` |
| **Stage transition** | PLAN_AUDIT → PRE_EXECUTION_SNAPSHOT |

### Step 8: TDD-First Gate (HUMAN GATE)

| Item | Detail |
|---|---|
| **Input** | `spec.md`, `plan.md` |
| **Actions** | Spawn testing-executor. Write tests only (no production code). Validate every criterion mapped. Present to user. Commit to snapshot branch. |
| **Receipts** | `tdd-tests` |
| **Cannot be skipped** | Hard rule |

### Step 9: Start Skill Done

| Item | Detail |
|---|---|
| **Stage transition** | → PRE_EXECUTION_SNAPSHOT |
| **Output** | "Foundry Ready to Execute" summary |

---

## 4. Phase 2: Execute Skill

### Step 0: Contract Validation

`dynosctl.py validate-contract --skill execute` — checks manifest, spec, plan, graph exist.

### Step 1-2: Review Plan + Snapshot Base

Read plan, capture HEAD SHA, create snapshot branch.

### Step 3: Execute Segments

| Sub-step | Detail |
|---|---|
| **Preflight** | `validate_task_artifacts.py` — structural checks |
| **Critical path** | Compute dependency depth, prioritize highest-depth segments |
| **Caching** | Skip segments with unchanged evidence |
| **Router call** | `dynorouter.py executor-plan` → model, route_mode, agent_path per segment |
| **Receipt: executor-routing** | Written BEFORE any spawns. **Required by CHECKPOINT_AUDIT gate.** |
| **Inject-prompt** | Pipes base prompt through `dynorouter.py inject-prompt` → appends learned agent rules + prevention rules. Logs `learned_agent_injected` event. |
| **Executor spawn** | With model from plan, prompt from inject-prompt |
| **Receipt: executor-{seg-id}** | Per segment. Records `learned_agent_injected`, tokens. Auto-records to `token-usage.json`. |
| **Post-batch** | Verify file ownership, evidence exists |

### Step 4: Run Tests

TDD suite first (blocking), then broader regression.

### Step 5: Handoff

Writes `handoff-execute-audit.json`.

---

## 5. Phase 3: Audit Skill

### Step 0-2: Contract Validation + Diff Scope

Validates contract. Runs `git diff --name-only {snapshot_sha}`.

### Step 3: Audit

| Sub-step | Detail |
|---|---|
| **Router call** | `dynorouter.py audit-plan` → which auditors, models, ensemble, skip |
| **Receipt: audit-routing** | Written BEFORE spawns |
| **Auditor selection** | Fast-track: spec-completion + security only. Normal: +code-quality, +dead-code, +domain-specific |
| **Skip policy** | Skip-exempt: security, spec-completion, code-quality. Others skip on zero-finding streak ≥ threshold |
| **Ensemble voting** | Security + db-schema: haiku+sonnet vote, escalate to opus if findings |
| **Learned injection** | Read agent .md, strip frontmatter, append as instructions |
| **Receipt: audit-{auditor}** | Per auditor. Auto-records tokens. |
| **Eager repair** | First blocking finding triggers immediate Phase 1 repair |

### Step 4: Two-Phase Repair Loop

**Phase 1:** Q-learning repair plan → coordinator → parallel batches → model escalation on retry ≥ 2 → incremental re-audit

**Phase 2:** Late findings + re-audit findings → same structure. Max 3 retries. Exceed → FAILED.

### Step 5: Gate to DONE

| Sub-step | Detail |
|---|---|
| **Retrospective** | `dynosctl.py compute-reward --write` → `task-retrospective.json` |
| **Receipt: retrospective** | Quality, cost, efficiency scores + total tokens |
| **Completion** | `transition_task(task_dir, "DONE")` |
| **DONE gate checks** | `task-retrospective.json` exists, audit reports exist, `retrospective` receipt, `post-completion` receipt |
| **Auto-fires** | `_fire_task_completed()` → event bus drain |
| **Receipt: post-completion** | Written by event bus after all handlers complete |

---

## 6. Post-Completion Event Bus Chain

Triggered automatically by `transition_task("DONE")`.

| Event | Handlers | Follow-on |
|---|---|---|
| `task-completed` | **learn** (dynopatterns.py), **trajectory** (dynostrajectory.py) | `learn-completed` |
| `learn-completed` | **evolve** (dynoevolve.py), **patterns** (dynopatterns.py) | `evolve-completed` |
| `evolve-completed` | **postmortem** (dynopostmortem.py), **improve** (dynopostmortem.py), **benchmark** (dynoauto.py) | `benchmark-completed` |
| `benchmark-completed` | **dashboard** (dynodashboard.py), **register** (dynoregistry.py) | (terminal) |

Each handler is timed and logged to `events.jsonl` with `eventbus_handler` event type.

---

## 7. Learn

Triggered by: `task-completed` event OR manual `/dynos-work:learn`.

| Step | What it does |
|---|---|
| Collect | Scans all `task-retrospective.json` files |
| EMA compute | Effectiveness scores over (role, model, task_type, source) quads, alpha=0.3 |
| Model Policy | Composite scoring (0.5q + 0.3c + 0.2e) with UCB1. Security forced to opus. |
| Skip Policy | Threshold = `3 + 2*(1-avg_quality)`, clamped [1,10]. Skip-exempt set enforced. |
| Write JSON | `model-policy.json`, `skip-policy.json`, `route-policy.json` |
| Write markdown | `dynos_patterns.md` to persistent dir + Claude Code memory |

---

## 8. Evolve

Triggered by: `learn-completed` event OR manual `/dynos-work:evolve`.

| Step | What it does |
|---|---|
| Gate check | ≥5 retrospectives required |
| Discover slots | Find (role, task_type) with ≥3 repairs and no existing agent |
| Generate | Write agent `.md` with learned rules. Register in `registry.json` with mode=shadow. |
| Benchmark | `dynobench.py run` → compare candidate vs baseline |
| Promote | shadow → alongside → replace (or demote/archive) |
| Pruning | Underperforming for 3 consecutive tasks → `.archive/` |

**Agent lifecycle:** shadow → alongside → replace (or → archive)

---

## 9. Receipt Chain

All receipts at `.dynos/task-{id}/receipts/{step-name}.json`.

| Receipt | Written by | Gates which transition |
|---|---|---|
| `plan-routing` | `receipt_plan_routing()` | Advisory |
| `planner-discovery` | `receipt_planner_spawn("discovery")` | Advisory |
| `planner-spec` | `receipt_planner_spawn("spec")` | Advisory |
| `spec-validated` | `receipt_spec_validated()` | Advisory |
| `planner-plan` | `receipt_planner_spawn("plan")` | Advisory |
| **`plan-validated`** | `receipt_plan_validated()` | **→ EXECUTION** |
| `plan-audit-check` | `receipt_plan_audit()` | Advisory |
| **`executor-routing`** | `receipt_executor_routing()` | **→ CHECKPOINT_AUDIT** |
| `executor-{seg-id}` | `receipt_executor_done()` | `validate_chain()` |
| `audit-routing` | `receipt_audit_routing()` | `validate_chain()` |
| `audit-{auditor}` | `receipt_audit_done()` | `validate_chain()` at DONE |
| **`retrospective`** | `receipt_retrospective()` | **→ DONE** |
| **`post-completion`** | `receipt_post_completion()` | **→ DONE** |

Bold = hard transition gate in `transition_task()`.

---

## 10. Token Recording Flow

Two paths, both write to `.dynos/task-{id}/token-usage.json`:

1. **Direct:** `dynoslib_tokens.py record` called by skills after spawns
2. **Receipt-piggyback:** `receipt_executor_done()` and `receipt_audit_done()` auto-call `_record_tokens()` when `tokens_used > 0`

Token data feeds into: retrospective scores → effectiveness EMA → model policy → UCB selection.

---

## 11. Learned Agent Injection Flow

**Write path (evolve):**
1. `dynoevolve.py` generates `.md` agent file from retrospective patterns
2. Registers in `registry.json` with mode=shadow
3. After benchmarking, promotes to alongside/replace

**Read path (execute/audit):**
1. Router checks `registry.json` for matching (role, task_type)
2. Returns route_mode + agent_path
3. `inject-prompt` reads `.md`, strips frontmatter, appends to executor prompt
4. Logs `learned_agent_injected` event
5. Receipt records `learned_agent_injected: true`

---

## 12. Router Decision Flow

Three questions for every agent:

| Question | Function | Decision logic |
|---|---|---|
| Which model? | `resolve_model()` | 1. Policy overrides. 2. UCB1 over EMA scores. 3. model-policy.json. 4. Default. 5. Security floor (security-auditor → opus). |
| Skip? | `resolve_skip()` | Skip-exempt set. Streak vs threshold. |
| Generic or learned? | `resolve_route()` | Registry lookup. File existence check. Shadow → generic. Security → never replaced. |

---

## 13. File System Layout

```
.dynos/
  events.jsonl                     # Global events (no task context)
  events/                          # Event bus event files
  task-{id}/
    manifest.json                  # Task state + classification
    raw-input.md                   # Original input
    discovery-notes.md             # Q&A
    design-decisions.md            # Design choices
    spec.md                        # Acceptance criteria
    plan.md                        # Implementation plan
    execution-graph.json           # Segment DAG
    execution-log.md               # Auto-populated timeline
    events.jsonl                   # Task-scoped structured events
    token-usage.json               # Token ledger
    task-retrospective.json        # Quality/cost/efficiency scores
    completion.json                # Completion record
    audit-summary.json             # Aggregated audit
    repair-log.json                # Repair assignments
    handoff-execute-audit.json     # Skill handoff
    receipts/                      # Proof artifacts
      plan-routing.json
      planner-discovery.json
      planner-spec.json
      planner-plan.json
      spec-validated.json
      plan-validated.json          # Gates EXECUTION
      plan-audit-check.json
      tdd-tests.json
      executor-routing.json        # Gates CHECKPOINT_AUDIT
      executor-{seg-id}.json
      audit-routing.json
      audit-{auditor}.json
      retrospective.json           # Gates DONE
      post-completion.json         # Gates DONE
    evidence/
      {seg-id}.md
      tdd-tests.md
    audit-reports/
      {auditor}-checkpoint.json

~/.dynos/projects/{slug}/
  policy.json
  model-policy.json
  skip-policy.json
  route-policy.json
  dynos_patterns.md
  trajectories.json
  autofix-metrics.json
  postmortems/
  learned-agents/
    registry.json
    executors/
    auditors/
    skills/
    .archive/
```
