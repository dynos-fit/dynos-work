<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
  <br />
  <sub>Built by <a href="https://dynos.fit">dynos.fit</a></sub>
</p>

# dynos-work

**Autonomous Software Foundry. Self-learning. Human-directed.**

Give it a task. It thinks, dreams, plans, executes, audits, repairs, and only marks complete when independent evidence proves every requirement is done — and learns from every cycle to get better over time.

No dependencies. No superpowers required.

---

## The problem

AI agents claim work is complete when it isn't.

They write code that mostly works, skip edge cases, leave stubs, miss requirements, and declare done. They also repeat the same mistakes run after run because they have no memory.

## The solution

dynos-work owns the full lifecycle — from a 10-word vision to a 1.0-Quality production system — and gets smarter with every task:

1. **Discover:** RL-informed interrogation using trajectory memory surfaces the right questions, not generic ones
2. **Dream:** MCTS architecture playouts in a simulation sandbox prove design options before you choose
3. **Spec:** normalizes acceptance criteria using all context from discovery and design
4. **Spec Review:** always pauses for your explicit sign-off. No auto-approval path
5. **Plan + Execution Graph:** generates implementation plan with dependency graph and parallel execution segments, bound by your design choices
6. **Plan Review:** pauses for your approval of the implementation plan
7. **Plan Audit:** spec-completion auditor verifies every acceptance criterion is addressed before any code is written
8. **Snapshot:** creates a git branch safety net before writing any code
9. **Execute:** dispatches specialized executor subagents in parallel, each receiving only its relevant segment, Gold Standard reference code, and prevention rules from project memory
10. **Test:** runs the project's test suite and gates on pass/fail before auditing
11. **Audit:** independent auditors run simultaneously with eager two-phase repair: repair begins as soon as the first auditor returns findings, while slower auditors continue running
12. **Repair:** converts findings to precise fixes with parallel batch execution, model escalation on retries, and domain-aware re-audit scoped to only affected files
13. **Gate:** only marks DONE when all auditors pass with evidence across both repair phases
14. **Reflect:** generates a structured retrospective from the task's audit and repair data
15. **Learn:** aggregates retrospectives into project memory, updates Model Policy, Skip Policy, and Gold Standard library — then calls Evolve
16. **Evolve:** promotes high-performing learned agents, retires underperformers, and runs proactive repo-wide meta-audits every 5 tasks

Agent self-reports are untrusted. Completion requires independent proof.

---

## Usage

### Three commands to ship

```
/dynos-work:start [task]   discover → dream → spec → plan → your approval
/dynos-work:execute        build → test → audit (fully automated)
/dynos-work:audit          checkpoint → repair loop → final gate → DONE
```

### Ongoing intelligence

```
/dynos-work:learn          aggregate task data → update policies → trigger evolve
/dynos-work:evolve         manage agent lifecycle → run proactive meta-audit
/dynos-work:maintain       autonomous debt scan → auto-fix PR → architectural tournament
/dynos-work:trajectory     push/pull SAR sequences for Decision Transformer memory
```

### When you need to intervene

```
/dynos-work:status         show current stage, audit results, open findings
/dynos-work:resume         resume an interrupted task
/dynos-work:plan           replan after scope changes or manual spec edits
/dynos-work:repair         fix a specific finding manually
/dynos-work:investigate    deep root cause analysis for any bug or error
```

---

## Lifecycle

```
DISCOVERY (RL-informed — state-encoder + trajectory search)
       → MCTS DREAMING (simulation sandbox for hard/critical design options)
       → SPEC_NORMALIZATION → SPEC_REVIEW (always, no skip)
                                   │
                          approved → PLANNING (Standard or Hierarchical Master/Worker)
                          changes  → re-normalize → SPEC_REVIEW
                                            │
                                   PLAN_REVIEW (always, no skip)
                                            │
                                   PLAN_AUDIT (spec-completion check before any code)
                                            │
         ┌──────────────────────────────────▼
         │  PRE_EXECUTION_SNAPSHOT (git branch safety net)
         │  EXECUTION (Critical Path Scheduler — parallel + incremental caching)
         │  TEST_EXECUTION (run test suite, gate on pass/fail)
         │  CHECKPOINT_AUDIT (adaptive auditor selection + diff-scoped)
         │      │
         │      ├── all pass → REFLECT → LEARN → EVOLVE → DONE
         │      └── findings → REPAIR PHASE 1 (eager, from first auditor) ─┐
         │                     REPAIR PHASE 2 (late + re-audit findings)    │
         │                                                                  │
         └──────────────────────────────────────────────────────────────────┘
                    (parallel batches, model escalation, max 3 retries per finding)
```

### Key behaviors

**RL-informed discovery:** the state-encoder produces a State Signature for the current codebase. The system searches `.dynos/trajectories.json` for the 3 most similar successful past trajectories and uses their known failure points to generate targeted questions — not generic ones.

**MCTS dreaming:** for any design option rated hard or critical, the Founder skill runs a Monte Carlo Tree Search playout in a `/tmp/` sandbox. It drafts the architecture, runs an Opus-level security and performance audit, and returns a Design Certificate (PASS/FAIL, security score, recommendation) before you choose.

**Mandatory spec sign-off:** you always review and approve the normalized spec before planning begins. No exceptions.

**Mandatory plan sign-off:** you always review and approve the implementation plan before any code is written. No exceptions.

**Adaptive auditing:** 3 skip-exempt auditors (spec-completion, security, code-quality) always run. Skip-eligible auditors (dead-code, ui, db-schema) are skipped based on learned skip thresholds from the policy table. Model selection for all agents is adaptive, driven by effectiveness scores tracked per (role, model, task_type, source) quad.

**Diff-scoped:** auditors only inspect files changed by the task, preventing false positives from pre-existing issues.

**Eager two-phase repair:** repair begins as soon as the first auditor returns findings (phase 1), while slower auditors continue running. Late auditor results feed into phase 2.

**Parallel repair batches:** the repair coordinator de-conflicts findings by file and groups them into non-overlapping batches that run concurrently. Only batches with file conflicts are serialized.

**Model escalation:** findings that fail repair twice (retry >= 2) automatically escalate the executor to Opus, trading tokens for higher success probability.

**Incremental caching:** if a segment's inputs (spec, plan, files) are unchanged from a previous run, the executor spawn is skipped and existing evidence is reused — reducing re-run cost by up to 80%.

**Critical Path Scheduling:** the execution engine calculates dependency depth for every segment and spawns the deepest (most blocking) segments first, preventing late-stage bottlenecks.

**Progressive pipelining:** high-risk domain segments (security, db) trigger background auditors immediately on completion, rather than waiting for all segments to finish.

**Rollback on failure:** if a task hits max retries, provides the snapshot branch and rollback commands.

---

## What runs under the hood

### The Foundry Intelligence Stack

| Layer | Component | Role |
|---|---|---|
| Memory | `state-encoder` agent + `trajectories.json` | Encodes codebase state; stores and retrieves SAR sequences |
| Imagination | `founder` skill (MCTS/Dreaming) | Simulates architecture options in sandbox before you choose |
| Coordination | `planning` agent (Hierarchical) | Master/Worker planning for high-risk or large-spec tasks |
| Execution | 7 executor specialists | Build code in parallel, guided by Gold Standards |
| Verification | 6 independent auditors | Zero-trust review, read-only, diff-scoped |
| Repair | `repair-coordinator` agent | De-conflicts and batches findings across all auditors |
| Learning | `learn` skill | Aggregates retrospectives, computes EMA policies |
| Evolving | `evolve` skill | Promotes/retires learned agents, runs proactive meta-audits |
| Maintenance | `maintain` skill | Autonomous background debt scanning and auto-fix PRs |

### Adaptive model selection

Model assignments are learned from task outcomes, not hardcoded. The policy table tracks effectiveness scores per (role, model, task_type, source) quad using exponential moving averages and selects the optimal model for each agent at spawn time using UCB scoring (exploration constant `c = 0.5`).

| Role | Default Model | Adaptive | Safety Floor |
|---|---|---|---|
| Planning | Opus | Yes | None |
| Repair coordinator | Sonnet | Yes | None |
| All 7 executors | Opus | Yes | None |
| Security auditor | Opus | Yes | Always Opus (monotonicity) |
| DB schema auditor | Opus | Yes | None |
| Spec-completion auditor | Sonnet | Yes | None |
| Code quality auditor | Sonnet | Yes | None |
| UI auditor | Sonnet | Yes | None |

Defaults are used during cold-start (first 5 tasks) or when the policy table is unavailable.

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

dynos-work learns from its own performance. Each completed task generates structured data that feeds back into future tasks — and compounds over time.

### How it works

1. **Reflect:** when a task reaches DONE, the audit gate produces `task-retrospective.json` containing finding counts, executor repair frequency, per-auditor zero-finding streaks, token usage per agent, model used per agent, and a reward vector (quality, cost, efficiency scores).

2. **Learn:** aggregates all retrospectives and updates `dynos_patterns.md` in project memory. Computes EMA effectiveness scores per (role, model, task_type, source) quad, derives a Model Policy, a Skip Policy, and a Gold Standard library. Validates through a meta-validator (bounds checking, monotonicity constraints, regression detection with rolling baseline revert). Every 10 tasks, also fetches official library documentation (RAG-Lite) to modernize prevention rules. Cross-project intelligence can be shared via `GLOBAL_DYNOS_MEMORY_PATH`.

3. **Evolve:** takes the updated patterns and manages the agent lifecycle. New candidate agents enter **Shadow Mode** — their findings are compared against generic agents across multiple tasks in a simulation environment before they are promoted to "Alongside" or "Replace" mode. Underperforming agents are retired. Every 5 tasks, a proactive repository-wide meta-audit identifies architectural drift and technical debt clusters not visible in task-scoped diffs.

4. **Prevent:** the execute skill reads prevention rules from `dynos_patterns.md`, filters by executor type, and injects matching rules into each executor's spawn instructions. This closes the loop: findings become rules, rules prevent findings.

5. **Maintain:** the maintain skill runs as a background worker. It performs autonomous debt polling (every 24 hours or on demand), opens auto-fix PRs for critical and high findings, and runs Architectural Tournaments — drafting 3 variant branches of a high-debt module in the sandbox, benchmarking all three for ROI/efficiency, and presenting the winner with full comparison data.

6. **Trajectory Memory:** the trajectory skill manages the Decision Transformer's SAR (State-Action-Reward) memory. After each task, it reconstructs the full sequence of states, actions, and rewards and pushes it to `.dynos/trajectories.json`. During the next discovery phase, the state-encoder retrieves the 3 most similar successful trajectories. Their "winning action sequences" provide a Policy Prior Bonus to MCTS tree branches and prune branches that historically led to low rewards.

### What it tracks

| Metric | Source | Used by |
|---|---|---|
| Top finding categories | Audit reports | Planner (spec normalization) |
| Executor repair frequency | Repair logs | Executors (self-checking) |
| Avg repair cycles by task type | Execution logs | Planner (risk assessment) |
| Prevention rules | Finding descriptions | Executors (pre-flight validation) |
| Spawn efficiency | Execution logs | Learn loop (waste tracking) |
| Per-auditor zero-finding streaks | Audit reports | Audit skill (skip decisions) |
| Effectiveness scores (EMA) | Reward vectors | Learn step (model selection) |
| Model Policy | Effectiveness scores | All spawners (adaptive model routing) |
| Skip Policy | Quality EMA | Audit skill (adaptive skip thresholds) |
| Baseline Policy | Policy snapshots | Meta-validator (regression revert) |
| Gold Standard instances | Perfect first-pass tasks | Executors (reference implementations) |
| Token usage per agent | Spawn metadata | Reward vector (cost signal) |
| SAR Trajectories | Task sequences | Discovery (RL-informed questions) |

Patterns are per-project and update automatically after each completed task. The system compounds improvements across five dimensions: prevention rules reduce findings, adaptive model selection reduces token cost, skip policies reduce unnecessary spawns, Gold Standards reduce repair cycles, and trajectory memory reduces architectural mistakes.

---

## State persistence

All task state is stored in `.dynos/task-{id}/` (gitignored). Tasks survive session restarts. Use `/dynos-work:resume` to continue interrupted work.

The execution log at `.dynos/task-{id}/execution-log.md` records every stage transition, subagent spawn, human gate, and decision with timestamps.

The dashboard at `.dynos/dashboard.html` provides a visual Engineering Control Center with finding density heatmaps and ROI charts.

---

## Installation

```
/plugin marketplace add HassamSheikh/dynos-work
/plugin install dynos-work
```

After installing, the following skills are available:

| Skill | Command | Purpose |
|---|---|---|
| start | `/dynos-work:start [task]` | Unified entry point — discovery, dreaming, spec, plan |
| execute | `/dynos-work:execute` | Build → test → audit |
| audit | `/dynos-work:audit` | Checkpoint → repair loop → final gate |
| learn | `/dynos-work:learn` | Aggregate retrospectives → update policies |
| evolve | `/dynos-work:evolve` | Agent lifecycle management + proactive meta-audit |
| maintain | `/dynos-work:maintain` | Autonomous background debt scanning + auto-fix PRs |
| trajectory | `/dynos-work:trajectory` | Push/pull SAR sequences for DT memory |
| status | `/dynos-work:status` | Show current stage, findings, open repairs |
| resume | `/dynos-work:resume` | Resume an interrupted task |
| plan | `/dynos-work:plan` | Replan after scope changes |
| repair | `/dynos-work:repair` | Manually fix a specific finding |
| investigate | `/dynos-work:investigate` | Deep root cause analysis |

---

## Contributing

See [CONTRIBUTING.md](.github/CONTRIBUTING.md).

---

## Philosophy

> Completion is determined only by independent audit backed by evidence.

Agent self-reports are untrusted. Completion requires every applicable auditor to pass with evidence, every acceptance criterion to have a file and line reference, and the repair loop to converge to zero blocking findings. The human is always the Director — the system's intelligence sharpens what you see, but every critical decision requires your explicit approval.

There is no shortcut.
