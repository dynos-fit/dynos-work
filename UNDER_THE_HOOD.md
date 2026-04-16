# Under The Hood

This page is for users who want the internal mechanics of `dynos-work`.

The short version:

- the user-facing workflow is skill-driven
- the safety and learning layers are runtime-driven
- learned behavior is gated by benchmarks, freshness, and rollback policy

## System Shape

`dynos-work` has three layers:

1. Skills and agents
2. Deterministic runtime control
3. Adaptive memory and evaluation

The skills define the workflow behavior. The runtime scripts enforce invariants around that behavior. The adaptive layer decides what should improve and what should stay in shadow.

## Task Lifecycle Internals

Each task lives under `.dynos/task-*/`.

Typical files:

- `manifest.json`
- `raw-input.md`
- `discovery-notes.md`
- `design-decisions.md`
- `spec.md`
- `plan.md`
- `execution-graph.json`
- `execution-log.md`
- `repair-log.json`
- `task-retrospective.json`

The runtime validates these artifacts before allowing the task to advance.

Main control runtime:

- `hooks/ctl.py`
- `hooks/validate_task_artifacts.py`
- `hooks/lib.py`

These enforce:

- stage transitions
- artifact shape
- acceptance coverage
- graph acyclicity
- segment ownership
- repair-log and retrospective validation

## Learning Model

This repo uses RL-inspired structure, not a classic trained RL policy.

What is actually implemented:

- state encoding
- trajectory storage and retrieval
- reward-like task scoring
- learned agent and skill registries
- shadow evaluation
- promotion and rollback
- freshness-gated routing

That means the system improves through measured outcomes, not through self-assertion.

## Memory And Retrieval

Two important pieces drive memory:

- `hooks/state.py`
- `hooks/trajectory.py`

`state.py` builds a deterministic state signature for the current repo or target slice.

`trajectory.py` manages `.dynos/trajectories.json`, which stores compact task traces derived from retrospectives. The system can search similar prior tasks and use them as advisory context during discovery and design review.

Retrieval never overrides current repo evidence.

## Design Review

`hooks/dream.py` is the design-review runtime.

It is an MCTS-lite evaluator over structured design options. It does not autonomously choose architecture. It produces advisory results that feed the human approval path.

## Learned Components

Learned components are stored under `.dynos/learned-agents/`.

They can be:

- agents
- skills

They move through explicit modes:

- `shadow`
- `alongside`
- `replace`

Registry state lives in:

- `.dynos/learned-agents/registry.json`

Main runtimes:

- `hooks/evolve.py`
- `hooks/eval.py`
- `hooks/generate.py`

`generate.py` creates structured learned markdown from observed task patterns and registers it automatically.

## Benchmarking And Promotion

Learned components are not trusted by default.

They have to win their route.

The benchmark layer supports:

- static benchmark fixtures
- sandbox command fixtures
- multi-step task fixtures
- repo-snapshot rollouts
- task-artifact-based challenger rollouts

Main runtimes:

- `hooks/bench.py`
- `hooks/rollout.py`
- `hooks/challenge.py`
- `hooks/fixture.py`

Supporting data:

- `.dynos/benchmarks/history.json`
- `.dynos/benchmarks/index.json`

Rules:

- challengers start in `shadow`
- promotion requires benchmark success
- must-pass category regressions block promotion
- active learned components can be demoted automatically
- stale learned routes can be blocked until refreshed

## Routing

Live routing is not controlled by markdown tables anymore.

It is controlled by the registry plus runtime policy.

Main runtime:

- `hooks/route.py`

Routing only uses a learned component if:

- it is allowed by registry mode
- it is not demoted
- it is not stale under freshness policy

Otherwise the system falls back to the generic built-in behavior.

## Automation

Shadow challengers and stale active routes are managed automatically.

Main runtime:

- `hooks/auto.py`
- `hooks/maintain.py`

What it does:

- synthesizes missing fixtures when possible
- queues matching challenger evaluations
- re-benchmarks stale active routes
- updates queue and status files
- can run as a persistent maintainer daemon when enabled by policy

Manual runtime trigger:

- `python3 hooks/maintain.py invoke --root .`

Supporting files:

- `.dynos/automation/queue.json`
- `.dynos/automation/status.json`
- `.dynos/policy.json`
- `.dynos/maintenance/status.json`
- `.dynos/maintenance/daemon.pid`

Freshness policy currently includes:

- `freshness_task_window`
- `active_rebenchmark_task_window`
- `shadow_rebenchmark_task_window`

## Observability

Three runtimes expose the adaptive system:

- `hooks/report.py`
- `hooks/lineage.py`
- `hooks/dashboard.py`

They provide:

- current learned-component state
- active routes
- shadow challengers
- demotions
- benchmark coverage gaps
- fixture and run traceability
- task -> component -> fixture -> run lineage
- local real-time dashboard artifacts

The dashboard lives at:

- `.dynos/dashboard.html`
- `.dynos/dashboard-data.json`

`dashboard.py` can also serve the dashboard locally for live refresh.

## Hooks

Two hooks keep the system warm:

- `hooks/session-start`
- `hooks/task-completed`

`session-start` refreshes dashboard artifacts and adds plugin context to the host.

It also attempts to ensure the maintainer daemon is running when `maintainer_autostart` is enabled in `.dynos/policy.json`.

`task-completed` triggers:

- learn
- automatic challenger evaluation
- dashboard refresh
- optional auto-commit when enabled in host settings

## Practical Reading

If you want to understand the repo in the shortest useful order, read:

1. `README.md`
2. `skills/start/SKILL.md`
3. `skills/execute/SKILL.md`
4. `skills/audit/SKILL.md`
5. `hooks/lib.py`
6. `hooks/auto.py`
7. `hooks/dashboard.py`

That gets you the public workflow, the control layer, the adaptive layer, and the live operator surface.
