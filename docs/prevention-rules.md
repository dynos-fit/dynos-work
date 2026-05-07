# Prevention Rules

Prevention rules encode failure modes discovered in real tasks. The rules engine
evaluates them at commit time (via a pre-commit hook) and at any stage-transition
gate. This document explains the enforcement model, how to add new rules, and how
to install the pre-commit hook.

## Enforcement model

### Structured-template rules

Rules with one of the six structured templates (`pattern_must_not_appear`,
`co_modification_required`, `every_name_in_X_satisfies_Y`, `signature_lock`,
`caller_count_required`, `import_constant_only`) are **mechanically enforced** by
the rules engine. The `enforcement` field on these entries records the _gate_ at
which the check runs (e.g. `ci-gate`, `static-check`, `lint`). The engine matches
the rule against real code or file changes and produces `Violation` objects.

Structured rules may have `enforcement` set to any value in the valid set:
`test`, `lint`, `static-check`, `runtime-guard`, `ci-gate`, `review-checklist`,
`prompt-constraint`.

### Advisory rules

Rules with `template: "advisory"` are **injected into executor prompts only**.
The engine never evaluates them against code — `check_advisory` always returns an
empty violation list. Advisory rules describe judgment-based constraints that
cannot be expressed structurally.

**Enforcement invariant for advisory rules:** An advisory rule must never carry an
enforcement label that implies mechanical checking (`ci-gate`, `static-check`,
`runtime-guard`, `lint`, `test`). The backfill pipeline detects such
"dishonest" labels and rewrites them to `prompt-constraint`. After the backfill,
no entry in the live registry has `template == "advisory"` paired with a demote-set
enforcement value.

## What the rules-check-passed receipt proves

A `rules-check-passed` receipt, emitted by `receipt_rules_check_passed`, proves:

- `rules_loaded`: the number of Rule objects successfully constructed from the
  registry at check time.
- `rules_skipped`: the number of entries skipped due to missing `rule_id` or
  `template` (malformed entries are warned and skipped, not fatal).
- `error_violations`: the number of violations with `severity == "error"`. A
  non-zero value here would have caused the hook to exit 1. A receipt with
  `error_violations: 0` proves the commit passed all enforced rules.

## How to add a new rule

Every rule entry requires the following fields:

| Field | Description |
|---|---|
| `rule_id` | Deterministic ID from `_generate_rule_id(rule, category)` |
| `template` | One of the seven valid template names |
| `rule` | Human-readable rule text (max 100 chars, imperative voice) |
| `category` | `sec`, `cq`, `dc`, `perf`, `comp`, `ui`, `db`, `test`, `process`, or `unknown` |
| `enforcement` | For structured templates: the gate; for advisory: `prompt-constraint` |

Additional recommended fields: `params` (template-specific), `executor`, `rationale`,
`source_finding`, `source_task`, `added_at`.

### Generating a rule_id

```python
from memory.postmortem_analysis import _generate_rule_id

rule_text = "Never call os.exit() in library code"
category = "security"
rule_id = _generate_rule_id(rule_text, category)
# e.g. "secu-35cfa937fa69"
```

The ID is deterministic: `<category[:4]>-<sha256(rule_text)[:12]>`.

### Example JSON entry

```json
{
  "rule_id": "secu-35cfa937fa69",
  "rule": "Never call os.exit() in library code",
  "category": "security",
  "template": "pattern_must_not_appear",
  "params": {
    "regex": "os\\.exit\\(",
    "scope": "*.py"
  },
  "enforcement": "ci-gate",
  "executor": "all",
  "rationale": "os.exit() bypasses cleanup and atexit handlers; use sys.exit() or raise SystemExit.",
  "source_finding": "finding-SEC-001",
  "source_task": "task-20260507-004",
  "added_at": "2026-05-07T00:00:00Z"
}
```

For an advisory rule:

```json
{
  "rule_id": "proc-a1b2c3d4e5f6",
  "rule": "Apply extra scrutiny to all SEC-class findings before closing",
  "category": "process",
  "template": "advisory",
  "params": {},
  "enforcement": "prompt-constraint",
  "executor": "all",
  "rationale": "SEC-class issues have repeatedly been under-verified in past tasks.",
  "source_finding": "recurring-pattern-sec",
  "source_task": "task-20260507-004",
  "added_at": "2026-05-07T00:00:00Z"
}
```

After editing the file, run `python3 hooks/rules_engine.py validate-rules` to
confirm the new entry passes schema validation.

## Installing the pre-commit hook

The pre-commit hook runs the rules engine against staged files before every commit.

```bash
python3 hooks/rules_engine.py install-hook
```

This creates `.git/hooks/pre-commit` with content byte-identical to the
`_HOOK_BODY` constant in `hooks/rules_engine.py`. The hook is idempotent: if the
dynos marker (`# dynos-rules-engine v1`) is present in the first 200 bytes of the
existing hook, the command exits 0 without writing.

**Note:** `.git/hooks/` is gitignored by git itself and is never committed to the
repository. Every contributor must re-run `python3 hooks/rules_engine.py
install-hook` after cloning or creating a fresh worktree.
