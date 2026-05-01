# Spec Writing Rules

## Don't combine measurement targets with structural changes

A spec must not require BOTH a measurement target AND a structural change for
the same entity. The structural change invalidates the measurement baseline,
making one or both ACs unreachable.

### Failure mode 1 — AC-19 (task-20260430-003)

AC-19 specified a line-count target for `_compute_bypassed_gates_for_force`.
A parallel AC promoted the function into a closure of its caller, removing
it from the module namespace. After the structural change, no addressable
function named `_compute_bypassed_gates_for_force` existed for the line-count
audit to measure — the AC was structurally unreachable.

### Failure mode 2 — AC-28 (task-20260430-003)

AC-28 specified a byte-identical `--help` output target. A parallel AC
restructured the `--help` argparse grouping. After the restructure, the byte
output of `--help` necessarily differed from the pre-task version — the
byte-identity AC and the restructure AC were mutually exclusive.

### Mitigation rule

Drop the measurement target, OR split into two tasks: rebaseline-then-refactor.

The rebaseline-then-refactor pattern: task N captures the new measurement
baseline AFTER the structural change lands and freezes it as the reference
fixture; task N+1 enforces the byte-identity / line-count rule against the
new baseline going forward.

### Automated detection

`hooks/spec_lint.py` detects this anti-pattern: any backtick-quoted entity
appearing in both a measurement-target AC (keywords: `at most`, `at least`,
`byte-identical`, `byte-for-byte`, `sha-256`, `unchanged`, `≤`, `lines`) and
a structural-change AC (keywords: `extract`, `split`, `promote`, `move`,
`restructure`, `refactor`, `decompose`) emits a finding. The linter runs as
part of `cmd_run_spec_ready` and blocks `respec_required` on unacked
findings. Per-AC bypass: `<!-- spec-lint: ack -->` on or adjacent to the AC
line. Whole-spec bypass: `<!-- spec-lint: ack-all -->` anywhere.
