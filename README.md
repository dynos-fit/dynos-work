<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
</p>

# dynos-work

A Claude Code plugin that checks its own work and learns from mistakes. Human-directed, tool-grounded, self-improving.

## Install

```
claude plugin marketplace add dynos-fit/dynos-work
claude plugin install dynos-work
```

## Use

```
/dynos-work:start build a user settings page
```

That's it. The plugin handles the rest: discovers what's needed, writes a spec, plans the work, builds it, audits it, and repairs anything it finds.

You approve twice (spec and plan), then it runs.

## Commands

```
/dynos-work:start [task]       start a new task
/dynos-work:execute            execute the approved plan
/dynos-work:audit              audit, repair, and finish
/dynos-work:status             check where your task is
/dynos-work:resume             continue after interruption
/dynos-work:investigate [bug]  deep bug investigation
```

## What it does

**Builds better.** Every task flows through 6 phases: intake, design, specification, planning, verification, and handoff. Nothing ships without independent verification from deterministic tools and adversarial auditors.

**Verifies with tools, not opinions.** API Contracts tables are cross-referenced against actual route definitions. Data Model tables are checked against real migrations. Doc paths are verified on disk. License compliance is scanned. The plan can't lie about what exists.

**Learns from itself.** After each task, it writes a retrospective with DORA-aligned metrics. Over time, it learns what works, what fails, and what to watch for. Your 10th task is better than your 1st. Disable learning entirely with `dynos config set learning_enabled false`.

**Enforces least privilege.** Each of the 17 agents declares its minimum tool set in frontmatter. Auditors cannot write files. Planners cannot execute commands. The security auditor can never be replaced by a learned agent.

**Shows you everything.** A dashboard across all your projects: quality trends, findings, costs, DORA metrics, what the system learned.

## Architecture

20 skills, 17 agents, 52 hooks. Three layers:

| Layer | What it does | Can be disabled? |
|---|---|---|
| **Core** | Spec, plan, execute, audit. The foundry. | No |
| **Learning** | Trajectory memory, learned agents, pattern extraction, Q-learning repair. | Yes (`dynos config set learning_enabled false`) |
| **Observability** | Dashboards, reports, lineage. Read-only. | Yes (delete the layer, nothing breaks) |

## CLI

```bash
dynos init                              # set up a project
dynos dashboard                         # start the dashboard server
dynos config set learning_enabled false # foundry-only mode
dynos config get                        # show all policy
dynos stats dora                        # DORA metrics from retrospectives
dynos stats usage                       # module usage telemetry
```

[Full CLI reference](INTERNALS.md#cli)

## How it works

```
You give it a task
    |
    v
Phase 1: Intake         --> discovers what's needed, you answer questions
Phase 2: Design         --> classifies, designs, fast-track gate
Phase 3: Specification  --> writes spec, you approve
Phase 4: Planning       --> writes plan + execution graph, you approve
Phase 5: Verification   --> gap analysis, plan audit, TDD-first tests
Phase 6: Handoff        --> ready for /dynos-work:execute
    |
    v
Execute                 --> parallel agents build it
Audit                   --> independent auditors verify it
Repair                  --> automatic (hard cap: 3 retries then escalate)
Retrospective           --> DORA metrics, reward scores, agent attribution
```

## Requirements

Just Claude Code. That's the only requirement.

## Links

- [Pipelines](PIPELINES.md)
- [Internals & CLI reference](INTERNALS.md)
- [Architecture](ARCHITECTURE.md)
- [Under the hood](UNDER_THE_HOOD.md)
- [Changelog](CHANGELOG.md)

## Philosophy

> Completion is a control decision, not a model opinion.

The system does not trust the model when it says the work is done. Every task requires independent evidence of completion. Wherever you can replace "LLM reviewing LLM output" with "deterministic tool checking LLM output," you should.
