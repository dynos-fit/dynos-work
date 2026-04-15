<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
</p>

# dynos-work

A multi-harness AI plugin that checks its own work, learns from mistakes, and fixes your code while you sleep.

Runs inside 18 AI coding harnesses: Claude Code, Cursor, Windsurf, Copilot, Codex, Gemini CLI, Kiro, Roo Code, Continue, Trae, OpenCode, CodeBuddy, Droid, Warp, Augment, Antigravity, Qoder, Kilocode.

## Install

```bash
npx dynos-work-cli init --ai <harness>
```

Replace `<harness>` with your AI coding harness (e.g. `claude`, `cursor`, `copilot`, `gemini`), or use `--ai all` to install into every detected harness. Running without `--ai` auto-detects what's in your project.

Claude Code users can also install as a native plugin:

```
claude plugin marketplace add dynos-fit/dynos-work
claude plugin install dynos-work
```

**Upgrading from 6.0.0?** See [docs/migration-7.0.md](docs/migration-7.0.md) — 7.0.0 moves the skill/agent sources into a template tree emitted by the CLI. Existing Claude installs need one `npx dynos-work-cli init --ai claude` run to regenerate.

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
/dynos-work:autofix on         enable background code scanning
/dynos-work:autofix off        disable it
```

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

Node 18+ (for `npx`) plus any of the 18 supported AI coding harnesses. Python 3.10+ is needed for the background daemon.

Claude Code gets the richest experience — parallel sub-agents, lifecycle hooks, per-agent model routing, structured questions. Other harnesses receive the same skills with capability-gated degradations (inlined agents, rewritten hook paths, plain-markdown prompts). See [docs/migration-7.0.md](docs/migration-7.0.md) for per-harness notes.

For autofix PRs: `gh` (GitHub CLI). For autofix code fixes inside Claude Code: `claude` CLI.

## Links

- [Migration to 7.0.0](docs/migration-7.0.md)
- [Pipelines](PIPELINES.md)
- [Internals & CLI reference](INTERNALS.md)
- [Architecture](ARCHITECTURE.md)
- [Under the hood](UNDER_THE_HOOD.md)
- [Changelog](CHANGELOG.md)

## Philosophy

> Completion is a control decision, not a model opinion.

The system does not trust the model when it says the work is done. Every task requires independent evidence of completion.
