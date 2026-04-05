<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
</p>

# dynos-work

A Claude Code plugin that checks its own work, learns from mistakes, and fixes your code while you sleep.

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

## What it does

**Builds better.** Every task goes through spec, plan, execute, audit. Nothing ships without independent verification.

**Learns from itself.** After each task, it writes a retrospective. Over time, it learns what works, what fails, and what to watch for. Your 10th task is better than your 1st.

**Fixes your code while you sleep.** Optional autofix scans your codebase for bugs, dead code, security issues, and dependency vulnerabilities. Opens PRs for safe fixes. Opens issues for risky ones.

**Shows you everything.** A dashboard across all your projects: quality trends, findings, costs, what the system learned.

## Power users

Want the CLI, daemons, dashboard server, and autofix scanner?

```bash
curl -sSL https://raw.githubusercontent.com/dynos-fit/dynos-work/main/install.sh | bash
```

Then:

```bash
dynos init --autofix    # set up a project with background scanning
dynos dashboard         # start the dashboard server
dynos autofix scan      # run a scan right now
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

The autofix runs separately in the background:

```
Scans your code (6 detectors including AI review)
    |
    v
Safe to fix?  --> opens PR automatically
Needs review? --> opens GitHub issue
```

## Requirements

Just Claude Code. That's the only requirement.

For autofix PRs: `gh` (GitHub CLI). For autofix code fixes: `claude` CLI.

## Links

- [Internals & CLI reference](INTERNALS.md)
- [Architecture](ARCHITECTURE.md)
- [Under the hood](UNDER_THE_HOOD.md)
- [Changelog](CHANGELOG.md)

## Philosophy

> Completion is a control decision, not a model opinion.

The system does not trust the model when it says the work is done. Every task requires independent evidence of completion.
