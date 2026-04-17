<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
  <br />
  <sub>Built by <a href="https://dynos.fit">dynos.fit</a></sub>
</p>

# dynos-work

`dynos-work` is a workflow plugin for AI-assisted software delivery.

It turns a task into a controlled lifecycle:

`discover -> spec -> plan -> execute -> test -> audit -> repair -> learn`

You do not need to know reinforcement learning, benchmarking internals, or any of the Python runtime tools to use it.

From the user side, it should feel simple:

1. give it a task
2. approve the spec and plan
3. let it build, test, audit, and repair
4. let it improve itself over time

## What It Optimizes For

- Human approval at product and planning gates
- Deterministic control around non-deterministic LLM work
- Low wasted effort through validation, reuse, and targeted execution
- Measurable self-improvement through retrospectives and benchmarks
- Regression resistance through shadow mode, promotion gates, and rollback

## Core Idea

Most coding-agent systems fail for one of two reasons:

1. They trust the model when it says the work is done.
2. They try to “learn” without any controlled evaluation loop.

`dynos-work` is built around the opposite assumptions:

- agent self-report is not trusted
- completion requires independent evidence
- learned behavior must earn production routing
- regression should block promotion by construction

## The User Surface

Most users only need these commands:

```text
/dynos-work:start [task]
/dynos-work:execute
/dynos-work:audit
/dynos-work:maintain
/dynos-work:status
/dynos-work:resume
```

What they mean:

- `/dynos-work:start` turns your request into discovery, spec, plan, and approval steps
- `/dynos-work:execute` carries out the approved plan
- `/dynos-work:audit` runs verification and repair loops until the task is clean or blocked
- `/dynos-work:maintain` manually triggers the maintainer for a maintenance cycle
- `/dynos-work:status` shows where the current task stands
- `/dynos-work:resume` continues interrupted work

There are more advanced skills in the repo, but the system is supposed to use its own internal tooling automatically. The README should not expect normal users to run low-level Python commands by hand.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│                        YOU (the user)                           │
│                                                                 │
│   /dynos-work:start "fix the login bug"                         │
│        │                                                        │
│        ▼                                                        │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │              FOUNDRY PIPELINE (per task)                │   │
│   │                                                         │   │
│   │   discover ─→ spec ─→ plan ─→ execute ─→ audit ─→ done │   │
│   │      │          │       │                         │     │   │
│   │   you answer  you     you                    retrospective  │
│   │   questions   approve approve                  written  │   │
│   └─────────────────────────────────────────────────────────┘   │
│        │                                                        │
│        ▼                                                        │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │              LEARNING LOOP (background daemon)          │   │
│   │                                                         │   │
│   │   trajectories ─→ patterns ─→ postmortems               │   │
│   │        ─→ improvements ─→ fixtures ─→ benchmarks         │   │
│   │                                                         │   │
│   │   Makes the system smarter over time.                   │   │
│   │   Runs every hour. No user action needed.               │   │
│   └─────────────────────────────────────────────────────────┘   │
│        │                                                        │
│        ▼                                                        │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │              AUTOFIX (optional, --autofix)              │   │
│   │                                                         │   │
│   │   scan codebase ─→ detect debt ─→ route by severity     │   │
│   │                                                         │   │
│   │   low/medium: worktree ─→ claude fix ─→ open PR         │   │
│   │   high/critical: open GitHub issue for human review     │   │
│   └─────────────────────────────────────────────────────────┘   │
│                                                                 │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │              DASHBOARD (dynos dashboard)                │   │
│   │                                                         │   │
│   │   All projects in one page. Quality trends, routes,     │   │
│   │   benchmarks, findings, daemon health, autofix PRs.     │   │
│   │   Click any project card for full detail.               │   │
│   └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

## How A Task Flows

### 1. Start

`/dynos-work:start [task]`

This creates a task directory under `.dynos/task-*/`, gathers context, writes normalized artifacts, and pauses for approval at the right places.

Typical outputs:

- `manifest.json`
- `raw-input.md`
- `discovery-notes.md`
- `design-decisions.md`
- `spec.md`
- `plan.md`
- `execution-graph.json`
- `execution-log.md`

### 2. Validate

Before execution, the control layer validates task artifacts deterministically:

- manifest shape
- spec headings and contiguous acceptance criteria
- plan structure
- execution graph coverage, ownership, dependencies, and cycles
- repair-log and retrospective shape when present

This happens as part of the system. Users should not need to know the internal validator commands.

### 3. Execute

`/dynos-work:execute`

Execution uses the validated `execution-graph.json` as the contract. Segments are executed in dependency order, can be cached when valid evidence already exists, and are checked against declared file ownership.

### 4. Audit And Repair

`/dynos-work:audit`

Auditors run independently. Findings are converted into repair work. Repairs re-enter audit until the task is either clean or blocked by policy.

Important properties:

- diff-scoped auditing
- domain-aware auditors
- eager repair loop
- deterministic ownership checks
- explicit stage transitions

### 5. Learn

After a task reaches `DONE`, the system writes a retrospective and updates its local memory.

That memory is then used for:

- trajectory retrieval
- prevention rules
- model and routing heuristics
- learned agent and skill evaluation
- future regression prevention

## Human In The Loop

This system is not meant to remove the human from critical decisions.

Human approval is still expected for:

- discovery clarifications
- spec approval
- plan approval
- TDD / contract-test approval

The learned system can influence choices. It does not replace those gates.

## What Is Deterministic

The strongest parts of the repo are runtime-enforced, not just prompt-described.

Implemented deterministic controls include:

- stage transition enforcement
- task artifact validation
- execution-graph integrity checks
- segment file-ownership checks
- benchmark scoring
- promotion and rollback policy
- route gating from live registry state
- freshness blocking for stale learned routes
- automation queueing for challengers
- local lineage and dashboard generation

## What Learns

This repo uses RL-inspired ideas in an LLM system. It is not a classic trained RL stack.

What actually exists:

- structured state encoding
- trajectory memory
- reward-like task scoring
- shadow evaluation
- benchmark-driven promotion
- automatic demotion on regression
- retrieval-conditioned decision support

What that means in practice:

- the system can reuse strong prior task patterns
- it can benchmark learned agents and skills against baselines
- it can refuse to route to a learned component unless benchmark evidence allows it
- it can fall back to generic behavior when learned routes are stale or regressing

## Learned Routing Model

Learned agents and skills move through explicit modes:

- `shadow`
- `alongside`
- `replace`

Promotion rules are benchmark-driven.

A challenger can only leave `shadow` if it clears policy thresholds. Fixtures can declare must-pass categories, and any regression in those categories blocks promotion. Active learned components can also be demoted automatically if later evaluations regress.

Routing uses the live registry, not markdown tables, as the source of truth.

## Benchmark And Freshness Policy

The repo now supports:

- benchmark history
- fixture traceability
- automatic fixture synthesis from completed tasks
- freshness-based route blocking
- re-benchmarking of stale active routes

If a learned route becomes stale, the runtime can block it and fall back to the generic component until refreshed evaluation exists.

## Dashboard

The dashboard is now a real local runtime surface, not just a static mockup.

Generate it:

```bash
/dynos-work:dashboard
```

Serve it locally with live refresh:

```bash
the system can generate and refresh the local dashboard automatically
```

Artifacts:

- `.dynos/dashboard.html`
- `.dynos/dashboard-data.json`

The dashboard is backed by live local JSON and refreshes from the local server. It shows:

- active routes
- queue pressure
- recent benchmark runs
- coverage gaps
- demotions
- lineage summary

## Manual Maintainer Trigger

If you want to run the maintainer manually, use:

```text
/dynos-work:maintain
```

That is the user-facing way to ask the system to run a maintenance pass immediately.

## Advanced Internals

The repo does include Python runtime tools for validation, routing, benchmarking, rollout evaluation, lineage, and dashboard generation.

Those are implementation details for the plugin and its agents.

Normal users should not need to learn or run them to benefit from the system.

If you do want the implementation details, read [WORKFLOW_TRACE.md](WORKFLOW_TRACE.md) — a function-by-function walkthrough of every pipeline step with the actual code and narrative.

If you want the contributor-oriented codebase map, read [ARCHITECTURE.md](ARCHITECTURE.md).

## Repo State

Operational state is stored under `.dynos/` and is gitignored.

Common runtime files:

- `.dynos/task-*/`
- `.dynos/trajectories.json`
- `.dynos/learned-agents/registry.json`
- `.dynos/benchmarks/history.json`
- `.dynos/benchmarks/index.json`
- `.dynos/automation/queue.json`
- `.dynos/automation/status.json`
- `.dynos/dashboard.html`
- `.dynos/dashboard-data.json`

## Multi-Project Setup

`dynos-work` supports a global daemon that maintains multiple registered projects from a single background process.

### Global Home

All cross-project state lives under `~/.dynos/`. This includes the project registry, aggregated stats, portable prevention rules, and daemon logs. Each project keeps its own `.dynos/` directory for local task state, trajectories, and learned components.

### Registering a Project

From within Claude Code, use the skill command:

```
/dynos-work:register
```

Or from any terminal (using the wrapper scripts in `bin/`):

```bash
dynos-registry register /path/to/your/project
dynos-registry list
dynos-registry status
```

Add `bin/` to your PATH for convenience:

```bash
export PATH="/path/to/dynos-work/bin:$PATH"
```

Other registry commands: `unregister`, `pause`, `resume`, `set-active`. Run `dynos-registry --help` for details.

### Starting and Stopping the Global Daemon

```bash
dynos-global start      # background daemon
dynos-global stop       # stop the daemon
dynos-global status     # check daemon health
dynos-global run-once   # single maintenance sweep
```

The daemon loops over all registered projects, runs maintenance cycles, and then sleeps before repeating. Idle projects receive exponential backoff so active projects get priority.

### Cross-Project Learning

The global daemon aggregates anonymous statistics and portable prevention rules across all registered projects. It does not share raw data, file paths, task content, learned agents, or project-specific patterns between projects.

Local project state always takes precedence. Global insights are additive: they inform maintenance and prevention but never override local decisions.

## CLI

All commands go through `dynos`.

### Setup

```bash
dynos init                        # register project + start daemon
dynos init --autofix              # with autofix enabled
```

### Project

```bash
dynos local start                 # start project daemon
dynos local start --autofix       # start with autofix
dynos local stop                  # stop daemon
dynos local status                # show daemon status
dynos local logs                  # cycle history
dynos local run-once              # run single cycle
dynos local dashboard             # per-project dashboard
```

### Global

```bash
dynos global start                # start cross-project sweeper
dynos global stop                 # stop sweeper
dynos global status               # show health
dynos global run-once             # single sweep
dynos global logs                 # sweep history
```

### Dashboard

```bash
dynos dashboard                   # start global dashboard server
dynos dashboard stop              # stop server
dynos dashboard restart           # restart server
```

### Autofix

```bash
dynos autofix scan                # scan all registered projects
dynos autofix scan /path          # scan one project (must be registered)
dynos autofix list                # show findings
dynos autofix clear               # reset findings
```

What it scans:
- Recurring audit findings from retrospectives
- Dependency vulnerabilities (pip-audit / npm audit)
- Dead code (unused imports, unreferenced functions)
- Architectural drift against learned patterns

What it does with findings:
- Low/medium: creates git worktree, invokes Claude to fix, opens PR
- High/critical: opens GitHub issue for human review
- Deduplicates against previous findings
- Max 2 attempts per finding, then permanently suppressed

### Projects

```bash
dynos list                        # list all registered projects
dynos remove                      # unregister current project
dynos pause                       # pause maintenance
dynos resume                      # resume maintenance
```

All accept an optional path or `--root /path` to target a specific project. Default is current directory.

### Internals

These are advanced commands used by the system internally. Most users never need them.

```bash
dynos ctl                         # task lifecycle, validation, approvals
dynos postmortem                  # postmortem generation + improvements
dynos patterns                    # regenerate learned patterns
dynos trajectory                  # trajectory store management
dynos route                       # routing decisions (model, auditor, agent)
dynos evolve                      # learned agent promotion lifecycle
dynos bench                       # benchmark runner
dynos report                      # project health reports
dynos plan                        # planning policy resolution
```

## Installation

### Most users: Plugin only

```
/plugin marketplace add HassamSheikh/dynos-work
```

That's it. You get the full task pipeline:

```
/dynos-work:start [task]          # discover, spec, plan, approve
/dynos-work:execute               # execute the plan
/dynos-work:audit                 # audit, repair, done
```

No CLI, no daemons, no setup. Just slash commands in Claude Code.

### Power users: CLI + daemons + dashboard + autofix

```bash
curl -sSL https://raw.githubusercontent.com/dynos-fit/dynos-work/main/install.sh | bash
source ~/.bashrc
```

This gives you the `dynos` CLI with everything: project daemons, global dashboard, autofix scanner, and all internal tools. See the [CLI](#cli) section above.

Or clone and install:

```bash
git clone https://github.com/dynos-fit/dynos-work.git
cd dynos-work
./install.sh
```

### Contributors

```bash
git clone https://github.com/dynos-fit/dynos-work.git
cd dynos-work
./install.sh --develop
```

Uses your local repo directly. Changes take effect immediately. Runs tests on setup.

### Requirements

- **Plugin only:** Claude Code
- **CLI install:** git, python3
- **Recommended:** `gh` (GitHub CLI, for autofix PRs/issues), `claude` (Claude CLI, for autofix code changes)
- **Optional:** `pip-audit` (dependency vulnerability scanning), `npm` (JS dependency scanning)

## Philosophy

`dynos-work` is strict on one point:

> completion is a control decision, not a model opinion

That is the reason the repo exists.

It tries to make AI coding systems useful by surrounding them with validation, audits, benchmarks, memory, and rollback paths instead of trusting them blindly.
