<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
  <br />
  <sub>Built by <a href="https://dynos.fit">dynos.fit</a></sub>
</p>

# dynos-work

**Audit-governed autonomous engineering system.**

Give it a task. It plans, executes, audits, repairs, and only marks complete when independent evidence proves every requirement is done.

No dependencies. No superpowers required.

---

## The problem

AI agents claim work is complete when it isn't.

They write code that mostly works, skip edge cases, leave stubs, miss requirements, and declare done. Standard audit tools just report findings but don't close the loop.

## The solution

dynos-work owns the full lifecycle:

1. **Intake** - understands your task, extracts acceptance criteria
2. **Plan** - generates implementation plan with dependency graph
3. **Execute** - dispatches specialized executor subagents in parallel
4. **Audit** - five independent auditors run simultaneously
5. **Repair** - converts findings to precise fixes, loops back to audit
6. **Gate** - only marks DONE when all auditors pass with evidence

Agent self-reports are untrusted. Completion requires independent proof.

---

## Usage

### Start any task

```
/dynos-work:start add a stripe checkout flow, products page, and success/cancel pages.
use the test keys from env. make it look clean.
```

That's it. dynos-work handles the rest.

### Power-user commands

```
/dynos-work:audit    # Audit current work
/dynos-work:status   # Show task progress and open findings
/dynos-work:repair   # Manually trigger repair on a finding
/dynos-work:resume   # Resume an interrupted task
```

---

## What runs under the hood

**Executor specialists (run your code):**
- UI executor: components, pages, interactions, styling
- Backend executor: APIs, services, auth, business logic
- ML executor: models, pipelines, inference
- DB executor: schema, migrations, indexes, queries
- Refactor executor: structural cleanup
- Testing executor: unit, integration, e2e tests
- Integration executor: wiring, plumbing, external APIs

**Auditors (verify independently, read-only):**
- Spec-completion: did every acceptance criterion get implemented?
- Security: injection, auth gaps, secrets, data exposure
- UI: states, interactions, accessibility, responsive behavior
- Code quality: structure, correctness, tests, maintainability
- DB schema: design, migration safety, indexes, integrity

Spec-completion and security run on every task. Others activate based on what was touched.

---

## State persistence

All task state is stored in `.dynos/task-{id}/` (gitignored). Tasks survive session restarts. Use `/dynos-work:resume` to continue interrupted work.

---

## Installation

### Claude Code

```
/plugin marketplace add HassamSheikh/dynos-work
/plugin install dynos-work
```

### Cursor

Search "dynos-work" in the Cursor plugin marketplace.

### Gemini CLI

```bash
gemini extensions install https://github.com/HassamSheikh/dynos-work
```

### OpenCode

Register `.opencode/plugins/dynos-work.js` in your OpenCode config.

### Codex

See `.codex/INSTALL.md` for manual setup instructions.

---

## Philosophy

> Completion is determined only by independent audit backed by evidence.

The Lifecycle Controller is the only entity that can write `DONE`. It only does so after every applicable auditor passes, every acceptance criterion has a file+line evidence reference, and the repair loop has converged to zero blocking findings.

There is no shortcut.


