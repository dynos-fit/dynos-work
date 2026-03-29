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

1. **Discover:** asks up to 5 targeted questions to surface gaps, trade-offs, and unstated constraints before any spec is written
2. **Design:** identifies critical and high-complexity subtasks, proposes 2-3 options each with pros and cons, and asks for your choice. Easy and medium subtasks are decided autonomously.
3. **Classify & Spec:** understands your task, classifies risk, extracts acceptance criteria using all context from discovery and design
4. **Spec Review:** always pauses for your explicit sign-off on the normalized spec before any code is written. No auto-approval path.
5. **Plan:** generates implementation plan with dependency graph, bound by your design choices
6. **Plan Review:** pauses for your approval of the implementation plan
7. **Plan Audit:** spec-completion auditor verifies every acceptance criterion is addressed in the plan before any code is written
8. **Snapshot:** creates a git branch safety net before writing any code
9. **Execute:** dispatches specialized executor subagents in parallel, with live progress tracking
10. **Test:** runs the project's test suite and gates on pass/fail before auditing
11. **Audit:** independent auditors run simultaneously, scoped to only the files you changed
12. **Repair:** converts findings to precise fixes, loops back through tests and audit
13. **Gate:** only marks DONE when all auditors pass with evidence

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
/dynos-work:status    show current stage, audit results, open findings
/dynos-work:resume    resume an interrupted task
/dynos-work:plan      replan after scope changes or manual spec edits
/dynos-work:repair    fix a specific finding manually
```

---

## Lifecycle

```
DISCOVERY (up to 5 questions) → DESIGN_OPTIONS (critical/hard subtasks only)
       → CLASSIFY_AND_SPEC → SPEC_REVIEW (always, no skip)
                                   │
                          approved → PLANNING → PLAN_REVIEW
                          changes  → re-normalize → SPEC_REVIEW
                                            │
                                   low risk: auto-approve
                                   med+ risk: user approval
                                            │
                                   PLAN_AUDIT (spec-completion check)
                                            │
         ┌──────────────────────────────────▼
         │  EXECUTION_GRAPH_BUILD (coordinator builds parallel batches)
         │  PRE_EXECUTION_SNAPSHOT (git branch safety net)
         │  EXECUTION (parallel executor batches with progress tracking)
         │  TEST_EXECUTION (run test suite, gate on pass/fail)
         │  CHECKPOINT_AUDIT (risk-based auditor selection + diff-scoped)
         │      │
         │      ├── all pass → FINAL_AUDIT → DONE
         │      └── failures → REPAIR_PLANNING → REPAIR_EXECUTION ─┐
         │                                                          │
         └──────────────────────────────────────────────────────────┘
                              (repair loop, max 3 retries per finding)
```

### Key behaviors

**Human-guided discovery:** before writing a single line of spec, dynos-work asks targeted questions and surfaces design trade-offs for the decisions that actually matter

**Mandatory spec sign-off:** you always review and approve the normalized spec before execution begins, regardless of risk level

**Risk-based auditing:** domain-relevant auditors (ui, code-quality, db-schema) always run when those domains are touched, regardless of risk level. high/critical runs all 6.

**Diff-scoped:** auditors only inspect files changed by the task, preventing false positives from pre-existing issues

**Evidence reuse:** on repair re-audit, auditors whose files weren't touched carry forward their previous pass

**Test gate:** test suite runs before audit to catch real failures before spending tokens on auditors

**Rollback on failure:** if a task hits max retries, provides the snapshot branch and rollback commands

**Execution log:** every stage transition, agent spawn, human gate, and decision is timestamped and written to `execution-log.md` for full traceability

---

## What runs under the hood

### Model tiering

Not every agent needs the most capable model. Orchestration and checklist agents use a fast model; code-writing and adversarial agents use the most capable model available on your platform.

| Role | Tier | Why |
|---|---|---|
| Planning | high | Deep reasoning for spec and plan |
| Execution coordinator | standard | Graph construction from spec |
| Repair coordinator | standard | Mapping findings to executors |
| All 7 executors | high | Writing production code |
| Security auditor | high | Adversarial thinking |
| DB schema auditor | high | Architectural judgment |
| Spec-completion auditor | standard | Checklist verification |
| Code quality auditor | standard | Pattern matching |
| UI auditor | standard | Checklist verification |

On Claude Code: `high` = Opus, `standard` = Sonnet. On other platforms, use the equivalent capability tier.

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
| Dead code | Unused imports, dead functions, orphaned files, commented-out code | Final audit only |

---

## State persistence

All task state is stored in `.dynos/task-{id}/` (gitignored). Tasks survive session restarts. Use `/dynos-work:resume` to continue interrupted work.

The execution log at `.dynos/task-{id}/execution-log.md` records every stage transition, subagent spawn, human gate, and decision with timestamps.

---

## Installation

### Claude Code

```
/plugin marketplace add HassamSheikh/dynos-work
/plugin install dynos-work
```

### Cursor

Search "dynos-work" in the Cursor plugin marketplace.

### Gemini CLI

```bash
gemini extensions install https://github.com/HassamSheikh/dynos-work
```

### OpenCode

Register `.opencode/plugins/dynos-work.js` in your OpenCode config.

### Codex

See `.codex/INSTALL.md` for manual setup instructions.

---

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md).

---

## Philosophy

> Completion is determined only by independent audit backed by evidence.

Completion requires every applicable auditor to pass with evidence, every acceptance criterion to have a file and line reference, and the repair loop to converge to zero blocking findings.

There is no shortcut.
