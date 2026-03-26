# dynos-audit

Spec-driven audit enforcement for AI coding agents. Requires [Superpowers](https://github.com/obra/superpowers).

## What it does

Installs alongside Superpowers and enforces spec auditing at every phase:

- After brainstorming → `spec-auditor` checks full spec coverage
- After writing-plans → `spec-auditor` checks every requirement maps to a task
- After each implementation task → `audit-router` detects context (UI/code/mixed) and dispatches the right auditors
- Before finishing-a-development-branch → `spec-auditor` final gate blocks merge until passing

## Installation

Requires Superpowers to be installed first.

**Claude Code:**
```bash
/plugin install superpowers
/plugin install dynos-audit
```

**Cursor:** Search for "dynos-audit" in the plugin marketplace.

**Gemini CLI:**
```bash
gemini extensions install https://github.com/hassam/dynos-audit
```

**OpenCode / Codex:** See platform-specific INSTALL.md files in `.opencode/` and `.codex/`.

## Skills

- `dynos-audit:audit-router` — detects context from files touched, dispatches correct auditors
- `dynos-audit:spec-auditor` — audits artifact against spec, loops until provably complete
- `dynos-audit:ui-auditor` — UI/UX completeness checklist (expanded in v2)
- `dynos-audit:code-quality-auditor` — code quality checklist (expanded in v2)

## Supported Platforms

Claude Code, Cursor, Gemini CLI, OpenCode, Codex

## Philosophy

Never trust claims. Audit against evidence at every phase.
