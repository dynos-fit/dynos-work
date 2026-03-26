# dynos-audit — Codex Installation

Codex does not support automatic plugin loading. Follow these steps to install dynos-audit manually.

## Step 1: Copy skills

Copy the dynos-audit skills into your Codex skills directory:

```bash
cp -r /path/to/dynos-audit/skills/* ~/.codex/skills/
```

## Step 2: Add audit rules to AGENTS.md

Add the following to your project's `AGENTS.md` file (create it at the project root if it doesn't exist):

```
<EXTREMELY_IMPORTANT>
dynos-audit is installed and active.

MANDATORY AUDIT RULES:

1. After brainstorming completes → invoke dynos-audit:spec-auditor
2. After writing-plans completes → invoke dynos-audit:spec-auditor
3. After each task in subagent-driven-development → invoke dynos-audit:audit-router
4. Before invoking finishing-a-development-branch → invoke dynos-audit:spec-auditor

dynos-audit:audit-router inspects files touched (git diff --name-only) and dispatches:
- UI files only (.tsx .jsx .css .html .vue .svelte) → spec-auditor + ui-auditor
- Logic files only (.ts .js .py .go .rs .java) → spec-auditor + code-quality-auditor
- Mixed → all three auditors

DO NOT claim any phase complete until the relevant auditors pass.
DO NOT skip audit because the work seems done.
DO NOT proceed to the next phase while any auditor has open gaps.
</EXTREMELY_IMPORTANT>
```

## Step 3: Verify

Start a Codex session and ask:

```
What audit rules are active?
```

Expected: Codex lists the four mandatory audit rules.
