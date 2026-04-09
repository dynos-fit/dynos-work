<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
</p>

# dynos-work

A Claude Code plugin that checks its own work and learns from mistakes.

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

**Builds better.** Every task goes through spec, plan, execute, audit. Nothing ships without independent verification.

**Learns from itself.** After each task, it writes a retrospective. Over time, it learns what works, what fails, and what to watch for. Your 10th task is better than your 1st.

**Shows you everything.** A dashboard across all your projects: quality trends, findings, costs, what the system learned.

## Power users

Want the CLI, daemons, and dashboard server?

```bash
curl -sSL https://raw.githubusercontent.com/dynos-fit/dynos-work/main/install.sh | bash
```

Then:

```bash
dynos init              # set up a project
dynos dashboard         # start the dashboard server
```

[Full CLI reference](INTERNALS.md#cli)

## How it works

```
You give it a task
    |
    v
Discovers what's needed --> you answer questions
Writes a spec           --> you approve
Plans the work          --> you approve
Executes the plan       --> parallel agents
Audits the result       --> independent auditors
Repairs any findings    --> automatic
Writes a retrospective  --> learns for next time
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

The system does not trust the model when it says the work is done. Every task requires independent evidence of completion.
