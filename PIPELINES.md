# Pipelines

`dynos-work` has three pipelines. Each one has a distinct trigger, lifecycle, and output.

```
Task Pipeline          the main workflow — build, verify, ship
Learn Pipeline         extract knowledge from completed tasks
Observability Pipeline real-time visibility into everything above
```

Each pipeline is standalone. They communicate through **events** and **validated contracts**, not inline calls or shared state.

## Decoupling Architecture

**Event bus.** Pipelines communicate through a file-based event bus (`.dynos/events/`). When a task completes, the `task-completed` hook emits a `task-completed` event. The drain runner processes subscribers in order: learn subscribes to `task-completed`, evolve subscribes to `learn-completed`, and so on. Each handler swallows errors independently.

**Contract enforcement.** Each skill has a `contract.json` with versioned input/output schemas. The runtime validates contracts at pipeline boundaries (`ctl.py validate-contract`). Skills in the task pipeline (start, execute, audit) write handoff records confirming contract fulfillment.

**Domain-split imports.** The Python runtime is split by domain: `lib_core` (shared), `lib_validate` (task), `lib_trajectory` (learn), `lib_registry` (learn), `lib_benchmark` (learn), `lib_queue` (observability), `lib_events` (events), `lib_contracts` (contracts). No cross-domain imports. The `lib.py` facade remains for backward compatibility.

**Event flow:**
```
task-completed → [learn, trajectory]
learn-completed → [evolve, patterns]
evolve-completed → [postmortem, improve, benchmark]
benchmark-completed → [dashboard, register]
```

They connect through artifacts: the task pipeline produces retrospectives, the learn pipeline consumes them, and observability reads all state.

```
                    ┌──────────────────────────────┐
                    │        TASK PIPELINE          │
                    │  start → execute → audit      │
                    └──────────────┬───────────────┘
                                  │
                      task-retrospective.json
                                  │
              ┌───────────────────┴───────────────────┐
              ▼                                        ▼
   ┌──────────────────┐                 ┌──────────────────────┐
   │  LEARN PIPELINE   │                 │ OBSERVABILITY PIPELINE│
   │  patterns, policy │                 │ status, dashboard     │
   └────────┬─────────┘                 └──────────────────────┘
            │
    dynos_patterns.md
            │
            └──→ Task Pipeline (routing, skip policy, model selection)
```

---

## 1. Task Pipeline

The core workflow. Turns a user request into audited, verified code.

### Trigger

```
/dynos-work:start [task description]
```

### Stages

```
START
  step 0   metadata + task directory creation
  step 1   discovery intake (user answers questions)
  step 2   discovery + design + classification
  step 2b  fast-track eligibility gate
  step 3   spec normalization
  step 4   spec review ← HUMAN APPROVAL
  step 5   plan generation
  step 6   plan review ← HUMAN APPROVAL
  step 7   plan audit (spec coverage check)
  step 8   TDD-first test generation ← HUMAN APPROVAL
  step 9   ready for execute

EXECUTE (/dynos-work:execute)
  step 1   review plan + execution graph
  step 2   create git snapshot branch
  step 3   execute segments in dependency order
             executor types: ui, backend, db, integration, ml, refactor, testing
  step 4   run test suite
  step 5   verify completion

AUDIT (/dynos-work:audit)
  step 1   find active task
  step 2   determine diff scope
  step 3   run conditional auditors
             always: spec-completion, security, code-quality, dead-code
             conditional: ui-auditor (if domains include ui)
             conditional: db-schema-auditor (if domains include db)
  step 4   two-phase repair loop
             phase 1: early findings → repair-coordinator → executors
             phase 2: late findings → repair-coordinator → executors
  step 5   gate to DONE
             write task-retrospective.json
             transition to DONE
             (learn, evolve, dashboard run via task-completed event bus)
```

### Contract Chain

```
start
  in:  task_description (user prompt)
  out: manifest.json, spec.md, plan.md, execution-graph.json, execution-log.md
       │
execute
  in:  manifest.json, spec.md, plan.md, execution-graph.json, dynos_patterns.md?
  out: evidence/{segment-id}.md, snapshot, test-results.json, execution-log.md
       │
audit
  in:  manifest.json, spec.md, execution-graph.json, evidence/*.md, snapshot.head_sha
  out: audit-reports/*.json, repair-log.json, task-retrospective.json
```

Each stage validates its required inputs before proceeding. The runtime (`ctl.py`, `lib_core.py`) enforces stage transitions and artifact shape.

### Agents

| Role | Agent | Spawn pattern |
|---|---|---|
| Planning | `planning` | single — sonnet |
| Execution | `ui-executor`, `backend-executor`, `db-executor`, `ml-executor`, `integration-executor`, `refactor-executor`, `testing-executor` | single — sonnet |
| Execution | `docs-executor` | single — haiku |
| Audit | `security-auditor`, `db-schema-auditor` | **ensemble** — haiku + sonnet in parallel; if either finds issues → escalate to opus |
| Audit | `spec-completion-auditor`, `code-quality-auditor`, `dead-code-auditor`, `performance-auditor`, `ui-auditor` | single — sonnet (may be sampled into ensemble) |
| Repair | `repair-coordinator` | single — sonnet |
| Investigation | `investigator` | single — sonnet |

**Ensemble voting** (`security-auditor` and `db-schema-auditor` always; others sampled): spawn haiku and sonnet in parallel. If both return zero findings → PASS. If either returns findings → discard both, escalate to opus. Opus verdict is final. Configurable via `.dynos/config/policy.json` (`ensemble_auditors`, `ensemble_voting_models`, `ensemble_escalation_model`).

The router may further override models based on learned policy and benchmark history.

### Artifacts

All live under `.dynos/task-{id}/`:

```
manifest.json              task metadata, stage, classification
raw-input.md               original user request
discovery-notes.md         Q&A from discovery
design-decisions.md        hard option analysis
spec.md                    normalized acceptance criteria
plan.md                    technical approach
execution-graph.json       segments, dependencies, executor assignments
execution-log.md           append-only stage log
evidence/{segment-id}.md   per-segment evidence
audit-reports/*.json       findings per auditor
repair-log.json            repair batches and executor assignments
task-retrospective.json    outcomes, scoring, reward vector
```

---

## 2. Learn Pipeline

Extracts knowledge from completed tasks. Derives policies that make the next task better than the last.

### Trigger

Automatically via the `task-completed` event bus after every task reaches DONE. There is no manual slash command for the learn pipeline — it runs in the background without user action.

### Stages

```
LEARN
  step 1   locate retrospectives (glob .dynos/task-*/task-retrospective.json)
  step 2   aggregate patterns
             top finding categories (ranked by count)
             executor reliability rankings
             average repair cycles by task type
             prevention rules (synthesized from findings, max 15, FIFO eviction)
             spawn efficiency metrics
  step 3   library documentation refresh (every 10 tasks)
  step 4   determine project memory path
  step 5   write dynos_patterns.md core sections
  step 5a  extract reward data from retrospectives
  step 5b  compute EMA effectiveness scores per (role, model, task_type, source)
  step 5c  derive Model Policy table
  step 5d  derive Skip Policy table
  step 5e  manage Baseline Policy
  step 5f  cold-start gate (defer policies if <5 tasks)
  step 6   done (print completion message)
  step 7   global pattern sync (if GLOBAL_DYNOS_MEMORY_PATH set)
  step 8   human insight gate (high-impact changes)

EVOLVE (triggered by learn-completed event)
  generate learned agents from observed patterns
  register in .dynos/learned-agents/registry.json
  evaluate challengers via benchmarks
  promote: shadow → alongside → replace
  demote on regression
  prune stale or underperforming agents
```

### Contract Chain

```
learn
  in:  task-retrospective.json[] (glob), audit-reports[] (glob), existing dynos_patterns.md?
  out: dynos_patterns.md, learned-agents/*.md
       │
evolve
  in:  dynos_patterns.md, task-retrospective.json, registry.json, benchmark fixtures/results
  out: learned-agents/{executors,auditors,skills}/*.md, registry.json
```

### Output: dynos_patterns.md

The single file that feeds back into every future task:

```
Top Finding Categories         what auditors find most often
Executor Reliability           which executors produce clean code
Average Repair Cycles          cost of different task types
Prevention Rules               patterns that caused failures (max 15)
Spawn Efficiency               agent spawn overhead
Gold Standard Instances        first-pass perfect tasks
Effectiveness Scores           EMA-based per (role, model, task_type, source)
Model Policy                   role x task_type → recommended model
Skip Policy                    auditor → skip threshold
Baseline Policy                regression detection fallback
Agent Routing                  learned component → routing mode
```

### Learned Component Lifecycle

```
shadow       runs alongside baseline, output discarded, only measured
alongside    runs alongside baseline, output compared
replace      replaces baseline, becomes the default

Promotion requires benchmark evidence.
Must-pass regressions block promotion.
Stale routes are blocked until re-benchmarked.
Active components can be auto-demoted.
```

---

## 3. Observability Pipeline

Provides visibility into task state, learned policies, system health, and cross-project metrics. Non-blocking, read-only.

### Entry Points

```
/dynos-work:status           current task state
/dynos-work:dashboard        terminal policy report
dynos dashboard              start global dashboard server (port 8766)
dynos local dashboard        per-project dashboard
```

### Components

**Task Status** (`/dynos-work:status`)

```
in:  manifest.json, spec.md, execution-graph.json, audit-reports, repair-log.json,
     test-results.json, execution-log.md
out: human-readable status report
       task ID, title, stage
       lifecycle progress
       execution progress per segment
       acceptance criteria coverage
       audit results and open findings
       test results
       repair cycle count
       next command suggestion
```

**Policy Dashboard** (`/dynos-work:dashboard`)

```
in:  dynos_patterns.md, task-retrospective.json[] (glob)
out: terminal report
       policy summary
       finding trends
       executor reliability scores
       repair cycle statistics
```

**Local Dashboard** (served HTML)

```
.dynos/dashboard.html         generated HTML dashboard
.dynos/dashboard-data.json    backing data (quality trends, routes, findings)
```

**Global Dashboard** (`dynos dashboard`)

```
serves: http://127.0.0.1:8766/global-dashboard.html
shows:  all registered projects unified
        quality trends, findings, costs
        learned component state
        daemon health
```

**Lineage Tracking**

```
task → component → fixture → benchmark run
hooks/lineage.py generates the lineage graph
```

### Contract Chain

```
status
  in:  manifest.json, spec.md?, execution-graph.json?, audit-reports?, repair-log.json?,
       test-results.json?, execution-log.md?
  out: status_report (string)

dashboard
  in:  dynos_patterns.md, task-retrospective.json[]?
  out: terminal_report (string)
```

### Global State

```
~/.dynos/registry.json    project registry (path, timestamps, status)
~/.dynos/global.log       daemon activity log
~/.dynos/daemon.pid       global daemon PID
```

### Local State

```
.dynos/dashboard-data.json              task metrics, quality trends
.dynos/dashboard.html                   local dashboard
.dynos/learned-agents/registry.json     component state, routing modes
.dynos/benchmarks/history.json          benchmark run history
.dynos/benchmarks/index.json            benchmark coverage
```

### Runtime

```
hooks/report.py              machine-readable status
hooks/lineage.py             lineage graph output
hooks/dashboard.py           local dashboard generation + serving
hooks/global_dashboard.py    global dashboard UI
hooks/daemon.py               global daemon lifecycle
hooks/registry.py            project registry management
```

---

## Pipeline Interactions

| From | To | Mechanism | Coupling |
|---|---|---|---|
| Task | Learn | `task-completed` event via event bus | Event (async) |
| Learn | Evolve | `learn-completed` event via event bus | Event (async) |
| Evolve | Benchmark | `evolve-completed` event via event bus | Event (async) |
| Benchmark | Dashboard | `benchmark-completed` event via event bus | Event (async) |
| Learn | Task | dynos_patterns.md read by router.py | Artifact (optional) |
| Task | Observability | task artifacts readable by status/dashboard | Artifact (read-only) |

All cross-pipeline communication is either event-driven (async, fail-tolerant) or artifact-based (optional, with graceful fallback to defaults).

## Validation

**Contract chain validation:**
```
python3 hooks/ctl.py validate-chain
```

Checks that the `output_schema` of each pipeline stage covers the `input_schema` fields required by the next stage.

**Per-skill contract validation:**
```
python3 hooks/ctl.py validate-contract --skill execute --task-dir .dynos/task-{id}
python3 hooks/ctl.py validate-contract --skill audit --task-dir .dynos/task-{id} --direction output
```

**Full dry-run:** `/dynos-work:dry-run` runs chain validation plus optional runtime validation against real task artifacts.

## Runtime Modules

```
hooks/lib_core.py          shared: constants, paths, JSON, task state
hooks/lib_validate.py      task pipeline: spec/plan/graph validation
hooks/lib_trajectory.py    learn pipeline: trajectory store, quality scoring
hooks/lib_registry.py      learn pipeline: learned agent registry
hooks/lib_benchmark.py     learn pipeline: fixture evaluation
hooks/lib_queue.py         observability: automation queue
hooks/lib_events.py        events: file-based event bus
hooks/lib_contracts.py     contracts: runtime contract validation
hooks/lib.py               facade: backward-compatible re-exports
```
