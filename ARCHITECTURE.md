# Architecture

This document is for contributors working on `dynos-work` itself.

It explains where behavior lives, how the runtime is split, and what design constraints matter when changing the system.

## Architecture Summary

`dynos-work` is built from three cooperating layers:

1. Workflow skills and agent definitions
2. Deterministic runtime control
3. Adaptive evaluation and observability

The core rule of the repo is:

> prompts can suggest behavior, but runtime code should enforce invariants

That rule is what separates the public workflow from the internal control plane.

## Layer 1: Skills And Agents

The workflow surface lives under:

- `skills/`
- `agents/`

Skills define the end-user lifecycle:

- `start`
- `plan`
- `execute`
- `audit`
- `learn`
- `evolve`
- `status`
- `resume`
- `dashboard`

Agent markdown files define specialist roles such as planners and state encoders.

Guideline:

- Use skills and agent docs to describe behavior, sequencing, and user-facing policy.
- Do not rely on markdown alone for safety-critical guarantees.

If a rule must be true regardless of model behavior, it belongs in runtime code.

## Layer 2: Deterministic Runtime

The runtime lives primarily in:

- `hooks/lib.py`
- `hooks/ctl.py`
- `hooks/validate_task_artifacts.py`

`lib.py` is the shared library for:

- stage definitions and legal transitions
- task artifact validation
- repair-log and retrospective validation
- benchmark scoring and policy comparison
- registry management
- automation queue helpers
- fixture indexing and traceability
- freshness and route gating

`ctl.py` is the control CLI for:

- task validation
- stage transitions
- next-command resolution
- active-task lookup
- ownership checks

Design rule:

- reusable logic belongs in `lib.py`
- narrow operator entrypoints belong in small CLIs under `hooks/`

Avoid putting non-trivial policy directly into multiple scripts. Centralize it in the shared library.

## Layer 3: Adaptive Evaluation

The adaptive layer is split into several focused runtimes:

### Memory

- `hooks/state.py`
- `hooks/trajectory.py`

These implement state signatures and trajectory retrieval from retrospectives.

### Design Review

- `hooks/dream.py`

This is the structured design evaluator. It is advisory, not authoritative.

### Learned Components

- `hooks/evolve.py`
- `hooks/eval.py`
- `hooks/generate.py`

These manage learned component creation, registration, and evaluation.

### Benchmarks And Rollouts

- `hooks/bench.py`
- `hooks/rollout.py`
- `hooks/challenge.py`
- `hooks/fixture.py`

These implement:

- authored fixtures
- synthesized fixtures
- sandbox benchmarks
- repo-snapshot rollouts
- task-artifact-based challenger runs

### Routing And Automation

- `hooks/route.py`
- `hooks/auto.py`
- `hooks/maintain.py`

These manage:

- live route resolution
- shadow challenger queueing
- stale route refresh
- automation priority
- persistent maintenance polling when enabled

### Observability

- `hooks/report.py`
- `hooks/lineage.py`
- `hooks/dashboard.py`

These provide:

- machine-readable status
- lineage graph output
- real-time dashboard artifacts and serving

## Data Model

### Task State

Task state lives in `.dynos/task-*/`.

Important files:

- `manifest.json`
- `spec.md`
- `plan.md`
- `execution-graph.json`
- `execution-log.md`
- `repair-log.json`
- `task-retrospective.json`

### Learned State

Learned state lives under:

- `.dynos/learned-agents/registry.json`
- `.dynos/trajectories.json`
- `.dynos/benchmarks/history.json`
- `.dynos/benchmarks/index.json`
- `.dynos/automation/queue.json`
- `.dynos/automation/status.json`
- `.dynos/policy.json`

### Dashboard State

The dashboard consumes:

- `.dynos/dashboard-data.json`
- `.dynos/dashboard.html`

## Invariants

When contributing, preserve these invariants:

### Task Safety

- Illegal stage transitions must fail.
- Malformed task artifacts must not silently advance.
- Execution graph cycles and uncovered criteria must be rejected.
- Segment ownership must stay enforceable.

### Learned Routing Safety

- New learned components start in `shadow`.
- Promotion must depend on benchmark evidence.
- Must-pass regressions must block promotion.
- Active regressions must be able to demote routes.
- Stale learned routes must not silently stay active forever.

### User Control

- Human approval gates should remain explicit at spec and plan boundaries.
- Learned behavior should optimize choices inside guardrails, not redefine guardrails.

## Multi-Project Architecture

### Global vs Local State

`dynos-work` separates state into two scopes:

**Local state** (`.dynos/` inside each project):
- Task directories, manifests, specs, plans, execution graphs
- Trajectories and retrospectives
- Learned agents and registry
- Benchmarks, automation queues, dashboard artifacts
- Policy overrides

**Global state** (`~/.dynos/`):
- `registry.json`: the project registry
- `global.log`: daemon activity log
- `daemon.pid`: PID file for the background daemon
- Aggregated anonymous statistics
- Portable prevention rules collected across projects

### What Is Shared Across Projects

The global daemon shares only:

- Anonymous aggregate statistics (task counts, success rates, timing distributions)
- Portable prevention rules (patterns that caused failures, stripped of project-specific context)

The global daemon does **not** share:

- File paths or directory structures
- Task content, specs, plans, or execution graphs
- Learned agents or skills
- Project-specific patterns, trajectories, or retrospectives
- Credentials or environment variables

This boundary is enforced by design: the daemon reads local `.dynos/` state but writes cross-project outputs only to `~/.dynos/` in anonymized form.

### Registry Schema

The registry lives at `~/.dynos/registry.json`:

```json
{
  "projects": [
    {
      "path": "/absolute/path/to/project",
      "registered_at": "2026-04-03T12:00:00Z",
      "last_active_at": "2026-04-03T14:30:00Z",
      "status": "active"
    }
  ]
}
```

Each entry tracks:

- `path`: absolute filesystem path to the project root
- `registered_at`: ISO timestamp of first registration
- `last_active_at`: ISO timestamp of last activity (updated on registration, resume, or set-active)
- `status`: one of `active` or `paused`

### Daemon Lifecycle

The global daemon follows this loop:

1. **Start**: `sweeper.py start` forks a background process, writes `~/.dynos/daemon.pid`
2. **Run loop**: the daemon iterates over all registered projects in `registry.json`
3. **Per-project maintenance**: for each active project, run a maintenance cycle (validation sweeps, stale route checks, automation queue processing)
4. **Backoff for idle projects**: projects whose `last_active_at` is old receive exponential backoff, so the daemon spends less time on dormant repos
5. **Cross-project aggregation**: after visiting all projects, aggregate anonymous stats and update portable prevention rules in `~/.dynos/`
6. **Sleep**: wait for the configured interval before repeating

The daemon can be stopped with `sweeper.py stop`, which sends SIGTERM to the PID in the pidfile. `sweeper.py run-once` executes a single sweep without looping.

### Runtime Files

| File | Purpose |
|---|---|
| `hooks/sweeper.py` | Global daemon: start, stop, status, run-loop, run-once |
| `hooks/registry.py` | Registry CLI: register, unregister, list, status, pause, resume, set-active |

Both tools expose `--help` for all subcommands.

## Extension Guidelines

### When Adding A New Runtime Script

Ask:

1. Is this enforcing an invariant?
2. Does this belong as shared logic in `lib.py` first?
3. Does it need tests covering both happy and blocking paths?
4. Does it create or mutate `.dynos/` state that should be documented?

### When Changing Skills

Ask:

1. Is this user-facing workflow guidance or runtime enforcement?
2. If it is enforcement, should it move to code?
3. Does the skill still describe what the runtime actually does?

### When Changing Learned Policy

Ask:

1. Does this increase auditability or reduce it?
2. Can it regress silently?
3. Does it need a benchmark or freshness policy update?
4. Should route resolution change, or only evaluation behavior?

## Testing Strategy

Current automated coverage lives mainly in:

- `tests/test_dynosctl.py`
- `tests/test_learning_runtime.py`
- `tests/test_dream_runtime.py`

Contributors should add tests whenever changing:

- stage control
- validation logic
- benchmark scoring
- promotion or demotion policy
- auto queueing behavior
- dashboard or lineage outputs

Preferred pattern:

- add small deterministic fixtures
- assert on JSON outputs
- avoid tests that depend on network or host-specific setup

## Recommended Reading Order

For contributors, the best order is:

1. `README.md`
2. `PIPELINES.md`
3. `INTERNALS.md`
4. `WORKFLOW_TRACE.md`
5. `skills/start/SKILL.md`
6. `skills/execute/SKILL.md`
7. `skills/audit/SKILL.md`
8. `hooks/lib.py`
9. `hooks/auto.py`
10. `hooks/dashboard.py`
11. `tests/test_learning_runtime.py`

## Contributor Principle

If you are deciding whether a rule belongs in a prompt or in code, bias toward code.

If you are deciding whether a learned behavior should be trusted by default, bias toward shadow mode.
