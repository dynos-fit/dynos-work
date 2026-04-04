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

If you do want the implementation details, read [UNDER_THE_HOOD.md](UNDER_THE_HOOD.md).

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

All commands go through a single entry point: `dynos <subcommand>`.

```bash
# Add to PATH (one-time, add to ~/.bashrc for permanence)
export PATH="/path/to/dynos-work/bin:$PATH"
```

### Project Daemon (`dynos local`)

Runs the background data pipeline for a single project: trajectories, patterns, postmortems, improvements, fixtures, automation queue, dashboard refresh. With `--autofix`, also scans for issues and auto-fixes them.

```bash
dynos local start --root .                # start daemon (data pipeline only)
dynos local start --root . --autofix      # start daemon + autonomous autofix
dynos local stop --root .                 # stop daemon
dynos local status --root .               # show daemon status (running, autofix, cycle count)
dynos local run-once --root .             # single cycle, no daemon
dynos local run-once --root . --autofix   # single cycle + autofix scan
dynos local logs --root .                 # show recent cycle history
```

`dynos maintain` is an alias for `dynos local`.

### Proactive Scanner (`dynos proactive`)

Scans for technical debt across four categories. Called automatically by the daemon when `--autofix` is enabled, or run manually.

```bash
dynos proactive scan --root .              # run scan now
dynos proactive scan --root . --max-findings 5  # cap findings per scan
dynos proactive list --root .              # show all tracked findings
dynos proactive clear --root .             # reset findings history
```

What it scans:
- **Recurring audit findings** from retrospectives (categories appearing in >50% of last 10 tasks)
- **Dependency vulnerabilities** via `pip-audit` / `npm audit`
- **Dead code** via Python AST (unused imports, unreferenced functions)
- **Architectural drift** against gold standard patterns

What it does with findings:
- Low/medium severity: creates git worktree, invokes Claude to fix, opens PR
- High/critical severity: opens GitHub issue for human review
- Deduplicates against previous findings (never re-processes same issue)
- Max 2 attempts per finding, then permanently suppressed

### Global Daemon (`dynos global`)

Orchestrates maintenance across all registered projects. Sweeps each project via subprocess (crash-isolated, 300s timeout per project).

```bash
dynos global start                         # start background sweeper
dynos global stop                          # stop sweeper
dynos global status                        # show daemon health
dynos global run-once                      # single sweep of all projects
dynos global logs                          # show recent sweep history
```

### Global Dashboard (`dynos global dashboard`)

Unified web dashboard showing all registered projects in one page.

```bash
dynos global dashboard                     # generate HTML only
dynos global dashboard generate            # same as above
dynos global dashboard serve               # generate + start HTTP server at :8766
dynos global dashboard serve --port 9000   # custom port
dynos global dashboard kill                # stop the server
dynos global dashboard restart             # kill + serve
```

Dashboard shows:
- Global daemon status and aggregate stats
- Per-project cards (click to expand full detail)
- Active routes, recent tasks, benchmark runs, findings, maintenance cycle history
- Autofix PRs per project
- Paused/archived projects dimmed at bottom

### Project Registry (`dynos registry`)

Manages which projects the global daemon maintains.

```bash
dynos registry register .                  # register current directory
dynos registry register /path/to/project   # register any project
dynos registry unregister /path/to/project # remove from registry
dynos registry list                        # show all registered projects
dynos registry status                      # show with daemon health
dynos registry pause /path/to/project      # pause maintenance
dynos registry resume /path/to/project     # resume maintenance
```

### Task Control (`dynos ctl`)

Manage task lifecycle, validate artifacts, approve improvements.

```bash
dynos ctl active-task --root .             # show current active task
dynos ctl validate-task .dynos/task-XXX    # validate task artifacts
dynos ctl transition .dynos/task-XXX STAGE # advance task stage
dynos ctl next-command .dynos/task-XXX     # what command to run next
dynos ctl check-ownership .dynos/task-XXX seg-1 file1 file2  # verify segment owns files
dynos ctl list-pending --root .            # show unapplied improvements
dynos ctl approve imp-ID --root .          # approve an improvement
```

### Postmortems (`dynos postmortem`)

Generate postmortems from completed tasks and propose improvements.

```bash
dynos postmortem generate --root . --task-id task-XXX  # single task
dynos postmortem generate-all --root .     # all tasks
dynos postmortem propose --root .          # propose improvements (dry run)
dynos postmortem improve --root .          # propose + apply safe improvements
dynos postmortem list-pending --root .     # show pending proposals
dynos postmortem approve ID --root .       # approve a specific proposal
```

### Routing (`dynos route`)

Deterministic routing decisions: which model, which auditors, which learned agents.

```bash
dynos route audit-plan --root . --task-type feature --domains ui
dynos route executor-plan --root . --task-type refactor --graph .dynos/task-XXX/execution-graph.json
dynos route resolve security-auditor feature --root .
```

### Patterns (`dynos patterns`)

Aggregate retrospectives into prevention rules, model policy, skip policy, agent routing.

```bash
dynos patterns --root .                    # generate dynos_patterns.md
```

### Planning (`dynos plan`)

Deterministic planning decisions.

```bash
dynos plan start-plan --root . --task-dir .dynos/task-XXX
dynos plan planning-mode --root . --task-dir .dynos/task-XXX
dynos plan task-policy --root . --task-dir .dynos/task-XXX
```

### Learned Agents (`dynos evolve`)

Manage the shadow/alongside/replace promotion lifecycle for learned agents.

```bash
dynos evolve auto --root .                 # post-task evolve check
dynos evolve init-registry --root .        # create registry if missing
dynos evolve register-agent --root . --name my-agent --role executor --task-type feature
```

### Trajectories (`dynos trajectory`)

Manage the trajectory store used for task similarity matching.

```bash
dynos trajectory rebuild --root .          # rebuild from retrospectives
dynos trajectory search --root . --query query.json  # find similar past tasks
```

### Per-Project Dashboard (`dynos dashboard`)

Generate or serve the per-project dashboard (single project view).

```bash
dynos dashboard generate --root .          # generate HTML
dynos dashboard serve --root .             # serve at :8765
dynos dashboard serve --root . --port 9000 # custom port
```

### Benchmarks (`dynos bench`)

Run benchmark fixtures to evaluate learned agents and skills.

```bash
dynos bench run --fixture path/to/fixture.json
```

### Reports (`dynos report`)

Generate project health reports.

```bash
dynos report --root .                      # generate report JSON
```

## Installation

```bash
/plugin marketplace add HassamSheikh/dynos-work
/plugin install dynos-work
```

After installing, add the CLI tools to your PATH (one-time):

```bash
echo 'export PATH="$HOME/.claude/plugins/marketplaces/dynos-work/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```

This gives you `dynos-global`, `dynos-registry`, `dynos-postmortem`, and `dynos-router` commands from any directory.

## Philosophy

`dynos-work` is strict on one point:

> completion is a control decision, not a model opinion

That is the reason the repo exists.

It tries to make AI coding systems useful by surrounding them with validation, audits, benchmarks, memory, and rollback paths instead of trusting them blindly.
