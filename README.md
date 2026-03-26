<a href="https://dynos.fit"><img src="dynos-logo-dark.svg" alt="dynos.fit" width="200"/></a>

Built by the team at [**dynos.fit**](https://dynos.fit)

# dynos-audit

Spec-driven audit enforcement for AI coding agents. Forces every phase of development to prove completion against your spec before moving on.

## The Problem

LLMs lie about being done.

They skip requirements, mark tasks complete without evidence, and present milestones as finished while gaps remain. Ask an AI agent to build a feature from a spec and it will confidently say "done" while missing edge cases, skipping error states, and leaving requirements unimplemented.

The bigger the spec, the worse it gets. Without enforcement, no phase is ever truly complete.

## What dynos-audit Does

`dynos-audit` installs alongside [Superpowers](https://github.com/obra/superpowers) (a workflow plugin for AI coding agents) and intercepts every phase transition with a mandatory audit loop.

At every checkpoint, it builds a requirement ledger from your spec, audits the current artifact against it, identifies gaps, delegates fixes, and re-audits. It loops until every requirement is provably done with evidence. It never stops at "I found issues." It never says "mostly complete."

No phase advances until the auditor passes.

## How It Works

When you give an agent a spec and ask it to build something, `dynos-audit` fires automatically at four points:

```
You provide spec
        ↓
brainstorming          → spec-auditor
                         Did the brainstorm cover the full spec?
        ↓
writing-plans          → spec-auditor
                         Does the plan map to every requirement?
        ↓
each implementation    → audit-router
task                     Inspects files touched, dispatches correct auditors
        ↓
finishing the branch   → spec-auditor
                         Final gate, blocks merge until passing
```

The `audit-router` looks at what files actually changed via `git diff --name-only` and routes to the right auditors:

| Files changed | Auditors dispatched |
|---|---|
| UI only (`.tsx`, `.jsx`, `.css`, `.html`, `.vue`, `.svelte`) | `spec-auditor` + `ui-auditor` |
| Code only (`.ts`, `.js`, `.py`, `.go`, `.rs`, `.java`, etc.) | `spec-auditor` + `code-quality-auditor` |
| Both | All three |

## Installation

Requires [Superpowers](https://github.com/obra/superpowers) to be installed first.

**Claude Code:**
```bash
/plugin install superpowers
/plugin install dynos-audit@claude-plugins-official
```

**Cursor:** Search for `dynos-audit` in the plugin marketplace.

**Gemini CLI:**
```bash
gemini extensions install https://github.com/hassam/dynos-audit
```

**OpenCode:** See [`.opencode/`](.opencode/) for plugin setup.

**Codex:** See [`.codex/INSTALL.md`](.codex/INSTALL.md) for manual install instructions.

## Skills

| Skill | When it runs | What it checks |
|---|---|---|
| `audit-router` | After each implementation task | Inspects git diff, classifies changed files as UI/Code/Full, dispatches the right auditors |
| `spec-auditor` | Every phase (brainstorm, plan, code) | Builds a requirement ledger from your spec, audits the artifact, delegates fixes, re-audits until everything is done with evidence |
| `code-quality-auditor` | When logic files change (`.ts`, `.js`, `.py`, etc.) | Spec coverage, correctness, edge cases, error handling, tests, structure, cleanup |
| `ui-auditor` | When UI files change (`.tsx`, `.jsx`, `.css`, etc.) | States (loading/empty/error/success), spec coverage, interactions, accessibility, responsive behavior |

## Supported Platforms

Claude Code, Cursor, Gemini CLI, OpenCode, Codex

## Philosophy

Never trust claims. Audit against evidence at every phase.
