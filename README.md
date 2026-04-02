<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
  <br />
  <sub>Built by <a href="https://dynos.fit">dynos.fit</a></sub>
</p>

# dynos-work

**Audit-governed autonomous engineering system.**

Give it a task. It plans, executes, audits, repairs, and only marks complete when independent evidence proves every requirement is done.

No dependencies. No superpowers required.

---

## The problem

AI agents claim work is complete when it isn't.

They write code that mostly works, skip edge cases, leave stubs, miss requirements, and declare done. Standard audit tools just report findings but don't close the loop.

## The solution

dynos-work owns the full lifecycle:

1. **Discover + Design + Classify:** asks up to 5 targeted questions, identifies critical subtasks with design options, and classifies risk — all in a single planner pass
2. **Spec:** normalizes acceptance criteria using all context from discovery and design
3. **Spec Review:** always pauses for your explicit sign-off on the normalized spec. No auto-approval path.
4. **Plan + Execution Graph:** generates implementation plan with dependency graph and parallel execution segments, bound by your design choices
5. **Plan Review:** pauses for your approval of the implementation plan
6. **Plan Audit:** spec-completion auditor verifies every acceptance criterion is addressed before any code is written
7. **Snapshot:** creates a git branch safety net before writing any code
8. **Execute:** dispatches specialized executor subagents in parallel, each receiving only its relevant segment and criteria
9. **Test:** runs the project's test suite and gates on pass/fail before auditing
10. **Audit:** independent auditors run simultaneously with eager two-phase repair: repair begins as soon as the first auditor returns findings, while slower auditors continue running
11. **Repair:** converts findings to precise fixes with parallel batch execution, model escalation on retries, and domain-aware re-audit scoped to only affected files
12. **Gate:** only marks DONE when all auditors pass with evidence across both repair phases
13. **Reflect:** generates a structured retrospective from the task's audit and repair data
14. **Learn:** aggregates retrospectives across tasks into project memory, informing future planning and execution

Agent self-reports are untrusted. Completion requires independent proof.

---

## Usage

### Three commands to ship

```
/dynos-work:start [task]   discovery → spec → plan → your approval
/dynos-work:execute        build → run code → tests (fully automated)
/dynos-work:audit          checkpoint → repair loop → final gate → DONE
```

### When you need to intervene

```
/dynos-work:status       show current stage, audit results, open findings
/dynos-work:resume       resume an interrupted task
/dynos-work:plan         replan after scope changes or manual spec edits
/dynos-work:repair       fix a specific finding manually
/dynos-work:investigate  deep root cause analysis for any bug or error
/dynos-work:learn        aggregate past task data into project memory
```

---

## Lifecycle

```
DISCOVERY + DESIGN + CLASSIFICATION (single planner pass)
       → SPEC_NORMALIZATION → SPEC_REVIEW (always, no skip)
                                   │
                          approved → PLANNING + EXECUTION_GRAPH → PLAN_REVIEW
                          changes  → re-normalize → SPEC_REVIEW
                                            │
                                   PLAN_AUDIT (spec-completion check)
                                            │
         ┌──────────────────────────────────▼
         │  PRE_EXECUTION_SNAPSHOT (git branch safety net)
         │  EXECUTION (parallel executor batches with progress tracking)
         │  TEST_EXECUTION (run test suite, gate on pass/fail)
         │  CHECKPOINT_AUDIT (conditional auditor selection + diff-scoped)
         │      │
         │      ├── all pass → REFLECT (retrospective) → DONE
         │      └── findings → REPAIR PHASE 1 (eager, from first auditor) ─┐
         │                     REPAIR PHASE 2 (late + re-audit findings)    │
         │                                                                  │
         └──────────────────────────────────────────────────────────────────┘
                    (parallel batches, model escalation, max 3 retries per finding)
```

### Key behaviors

**Human-guided discovery:** before writing a single line of spec, dynos-work asks targeted questions and surfaces design trade-offs for the decisions that actually matter

**Mandatory spec sign-off:** you always review and approve the normalized spec before execution begins, regardless of risk level

**Conditional auditing:** 3 skip-exempt auditors (spec-completion, security, code-quality) always run. Skip-eligible auditors (dead-code, ui, db-schema) are skipped when they have a zero-finding streak of 3+ consecutive tasks, saving tokens without sacrificing safety.

**Diff-scoped:** auditors only inspect files changed by the task, preventing false positives from pre-existing issues

**Eager two-phase repair:** repair begins as soon as the first auditor returns findings (phase 1), while slower auditors continue running. Late auditor results feed into phase 2. Critical spec failures trigger immediate short-circuit to repair.

**Parallel repair batches:** non-overlapping repair batches run concurrently. Only batches with file conflicts are serialized.

**Model escalation:** findings that fail repair twice (retry >= 2) automatically escalate the executor to Opus, trading tokens for higher success probability.

**Domain-aware repair:** on repair re-audit, only auditors whose domain was touched by the repair re-run, plus spec-completion and security always

**Incremental re-audit:** re-audit after repair scopes file inspection to only repair-modified files, reducing re-audit context by up to 90%. Spec-completion auditor retains full scope since it checks overall requirement coverage.

**Pre-flight validation:** every executor runs a "Validate Before Done" checklist before writing evidence, with static baseline checks tailored per executor type plus dynamic prevention rules from project memory

**Test gate:** test suite runs before audit to catch real failures before spending tokens on auditors

**Rollback on failure:** if a task hits max retries, provides the snapshot branch and rollback commands

**Execution log:** every stage transition, agent spawn, human gate, and decision is timestamped and written to `execution-log.md` for full traceability

---

## What runs under the hood

### Model tiering

Not every agent needs the most expensive model. Orchestration and checklist agents use Sonnet; code-writing and adversarial agents use Opus.

| Role | Model | Why |
|---|---|---|
| Planning | Opus | Deep reasoning for spec, plan, and execution graph |
| Repair coordinator | Sonnet | Mapping findings to executors |
| All 7 executors | Opus | Writing production code |
| Security auditor | Opus | Adversarial thinking |
| DB schema auditor | Opus | Architectural judgment |
| Spec-completion auditor | Sonnet | Checklist verification |
| Code quality auditor | Sonnet | Pattern matching |
| UI auditor | Sonnet | Checklist verification |

### Executor specialists (run your code)

| Executor | Responsibility |
|---|---|
| UI executor | Components, pages, interactions, styling |
| Backend executor | APIs, services, auth, business logic |
| ML executor | Models, pipelines, inference |
| DB executor | Schema, migrations, indexes, queries |
| Refactor executor | Structural cleanup, no behavior changes |
| Testing executor | Unit, integration, e2e tests |
| Integration executor | Wiring, plumbing, external APIs |

### Auditors (verify independently, read-only)

| Auditor | What it checks | When it runs |
|---|---|---|
| Spec-completion | Did every acceptance criterion get implemented? | Every task |
| Security | Injection, auth gaps, secrets, data exposure | Every task |
| UI | States, interactions, accessibility, responsive behavior | UI domain touched |
| Code quality | Structure, correctness, tests, maintainability | Backend domain touched |
| DB schema | Design, migration safety, indexes, integrity | DB domain touched |
| Dead code | Unused imports, dead functions, orphaned files, commented-out code | Every task |

---

## Self-improvement

dynos-work learns from its own performance. Each completed task generates structured data that feeds back into future tasks.

### How it works

1. **Reflect:** when a task reaches DONE, the audit gate produces `task-retrospective.json` containing finding counts by auditor and category, executor repair frequency, spec review iterations, repair cycle count, subagent spawn count, wasted spawns, and per-auditor zero-finding streaks.

2. **Learn:** automatically aggregates all retrospectives in the current project after each completed task. Writes `dynos_patterns.md` to Claude Code's project memory, which is auto-loaded into every future conversation. Synthesizes actionable prevention rules from recurring finding descriptions. Can also be run manually with `/dynos-work:learn`.

3. **Prevent:** the execute skill reads prevention rules from `dynos_patterns.md`, filters by executor type, and injects matching rules into each executor's spawn instructions. Executors treat injected rules as mandatory pre-evidence checks alongside their static baseline checklist. This closes the loop: findings become rules, rules prevent findings.

### What it tracks

| Metric | Source | Used by |
|---|---|---|
| Top finding categories | Audit reports | Planner (spec normalization) |
| Executor repair frequency | Repair logs | Executors (self-checking) |
| Avg repair cycles by task type | Execution logs | Planner (risk assessment) |
| Prevention rules | Finding descriptions | Executors (pre-flight validation) |
| Spawn efficiency | Execution logs | Learn loop (waste tracking) |
| Per-auditor zero-finding streaks | Audit reports | Audit skill (auditor skip decisions) |

Patterns are per-project and update automatically after each completed task. The prevention rules compound over time: fewer findings lead to fewer repair cycles, fewer wasted spawns, and lower token costs. Auditor skip streaks compound similarly: auditors that consistently find nothing are automatically skipped, reducing spawn count and cost. Run `/dynos-work:learn` manually to force a refresh or after importing retrospective data.

---

## State persistence

All task state is stored in `.dynos/task-{id}/` (gitignored). Tasks survive session restarts. Use `/dynos-work:resume` to continue interrupted work.

The execution log at `.dynos/task-{id}/execution-log.md` records every stage transition, subagent spawn, human gate, and decision with timestamps.

---

## Installation

```
/plugin marketplace add HassamSheikh/dynos-work
/plugin install dynos-work
```

---

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md).

---

## Philosophy

> Completion is determined only by independent audit backed by evidence.

Completion requires every applicable auditor to pass with evidence, every acceptance criterion to have a file and line reference, and the repair loop to converge to zero blocking findings.

There is no shortcut.
