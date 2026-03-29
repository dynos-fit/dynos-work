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

1. **Discover** — asks up to 5 targeted questions to surface gaps, trade-offs, and unstated constraints before any spec is written
2. **Design** — identifies critical and high-complexity subtasks, proposes 2-3 options each with pros and cons, and asks for your choice. Easy and medium subtasks are decided autonomously.
3. **Classify & Spec** — understands your task, classifies risk, extracts acceptance criteria using all context from discovery and design
4. **Plan** — generates implementation plan with dependency graph, bound by your design choices
5. **Spec Review** — always pauses for your explicit sign-off on the normalized spec before any code is written. No auto-approval path.
6. **Plan Review** — for medium and higher risk tasks, pauses for your approval of the implementation plan
7. **Snapshot** — creates a git branch safety net before writing any code
8. **Execute** — dispatches specialized executor subagents in parallel, with live progress tracking
9. **Test** — runs the project's test suite and gates on pass/fail before auditing
10. **Audit** — independent auditors run simultaneously, scoped to only the files you changed
11. **Repair** — converts findings to precise fixes, loops back through tests and audit
12. **Gate** — only marks DONE when all auditors pass with evidence

Agent self-reports are untrusted. Completion requires independent proof.

---

## Usage

### Start any task

```
/dynos-work:start add a stripe checkout flow, products page, and success/cancel pages.
use the test keys from env. make it look clean.
```

That's it. dynos-work handles the rest.

### Power-user commands

```
/dynos-work:audit    # Audit current work (risk-based scoping)
/dynos-work:status   # Show task progress, execution segments, and open findings
/dynos-work:repair   # Manually trigger repair on a finding
/dynos-work:resume   # Resume an interrupted task
```

---

## Lifecycle

```
INTAKE → DISCOVERY (up to 5 questions) → DESIGN_OPTIONS (critical/hard subtasks only)
       → CLASSIFY_AND_SPEC → PLANNING → SPEC_REVIEW (always, no skip)
                                            │
                                   approved → PLAN_REVIEW
                                   changes  → re-normalize → SPEC_REVIEW
                                            │
                                   low risk: auto-approve
                                   med+ risk: user approval
                                            │
         ┌──────────────────────────────────▼
         │  PRE_EXECUTION_SNAPSHOT (git branch safety net)
         │  EXECUTION (parallel executor batches with progress tracking)
         │  TEST_EXECUTION (run test suite, gate on pass/fail)
         │  CHECKPOINT_AUDIT (risk-based auditor selection + diff-scoped)
         │      │
         │      ├── all pass → FINAL_AUDIT → COMPLETION_REVIEW → DONE
         │      └── failures → REPAIR_PLANNING → REPAIR_EXECUTION ─┐
         │                                                          │
         └──────────────────────────────────────────────────────────┘
                              (repair loop, max 3 retries per finding)
```

### Key behaviors

- **Human-guided discovery** — before writing a single line of spec, dynos-work asks targeted questions and surfaces design trade-offs for the decisions that actually matter
- **Mandatory spec sign-off** — you always review and approve the normalized spec before execution begins, regardless of risk level
- **Risk-based auditing** — low-risk tasks get 2 auditors (spec + security), medium adds domain-relevant, high/critical runs all 5
- **Diff-scoped** — auditors only inspect files changed by the task, preventing false positives from pre-existing issues
- **Evidence reuse** — on repair re-audit, auditors whose files weren't touched carry forward their previous pass
- **Test gate** — test suite runs before audit to catch real failures before spending tokens on auditors
- **Rollback on failure** — if a task hits max retries, provides the snapshot branch and rollback commands
- **Progress tracking** — manifest.json tracks segment completion during execution for live status

---

## What runs under the hood

### Model tiering

Not every agent needs the most expensive model. Orchestration and checklist agents use Sonnet; code-writing and adversarial agents use Opus.

| Role | Model | Why |
|---|---|---|
| Lifecycle controller | Sonnet | Orchestration logic, no code writing |
| Planning | Opus | Deep reasoning for spec and plan |
| Execution coordinator | Sonnet | Graph construction from spec |
| Repair coordinator | Sonnet | Mapping findings to executors |
| All 7 executors | Opus | Writing production code |
| Security auditor | Opus | Adversarial thinking |
| DB schema auditor | Opus | Architectural judgment |
| Spec-completion auditor | Sonnet | Checklist verification |
| Code quality auditor | Sonnet | Pattern matching |
| UI auditor | Sonnet | Checklist verification |

### Executor specialists (run your code)
- **UI executor** — components, pages, interactions, styling
- **Backend executor** — APIs, services, auth, business logic
- **ML executor** — models, pipelines, inference
- **DB executor** — schema, migrations, indexes, queries
- **Refactor executor** — structural cleanup (no behavior changes)
- **Testing executor** — unit, integration, e2e tests
- **Integration executor** — wiring, plumbing, external APIs

### Auditors (verify independently, read-only)
- **Spec-completion** — did every acceptance criterion get implemented? (runs on every task)
- **Security** — injection, auth gaps, secrets, data exposure (runs on every task)
- **UI** — states, interactions, accessibility, responsive behavior
- **Code quality** — structure, correctness, tests, maintainability
- **DB schema** — design, migration safety, indexes, integrity

---

## State persistence

All task state is stored in `.dynos/task-{id}/` (gitignored). Tasks survive session restarts. Use `/dynos-work:resume` to continue interrupted work.

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

## Philosophy

> Completion is determined only by independent audit backed by evidence.

The Lifecycle Controller is the only entity that can write `DONE`. It only does so after every applicable auditor passes, every acceptance criterion has a file+line evidence reference, and the repair loop has converged to zero blocking findings.

There is no shortcut.
