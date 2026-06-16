# Circuit Breaker

The circuit breaker module (`hooks/circuit_breaker.py`) encodes runtime conditions under which a task should be hard-aborted or downgraded. It is wired into two lifecycle checkpoints in `hooks/ctl.py` and operates in observe-only mode by default.

## What the breaker enforces

Six conditions are evaluated across two stages:

**EXECUTION stage** (evaluated in this priority order):

1. `wasted_spawns_abort` — wasted spawn count >= `WASTED_SPAWN_ABORT_THRESHOLD` (9); task is burning tokens with no useful output.
2. `small_task_token_overrun` — task classified as bugfix/feature with <= 2 unique files and token usage >= `SMALL_TASK_TOKEN_LIMIT` (9,371,185).
3. `bugfix_token_overrun` — task classified as bugfix and token usage >= `BUGFIX_TOKEN_LIMIT` (68,283,719).
4. `small_task_token_downgrade` — small task token usage >= `SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD` (7,496,948) but < `SMALL_TASK_TOKEN_LIMIT` (9,371,185) (warning tier).
5. `bugfix_token_downgrade` — bugfix token usage >= `BUGFIX_TOKEN_DOWNGRADE_THRESHOLD` (54,626,975) but < `BUGFIX_TOKEN_LIMIT` (68,283,719) (warning tier).

**AUDITING stage**:

6. `opus_auditor_zero_yield` — >= 3 opus audit spawns whose audit-report has empty findings, after receipt-provenance cross-check (SEC-CB-001).

Conditions 1–3 and 6 return an abort decision (`abort=True`). Conditions 4–5 return a downgrade decision (`action="downgrade"`).

## Observe-only design

`BREAKER_ACTIVE: bool = False` at the top of `hooks/circuit_breaker.py` controls whether the breaker acts or merely observes.

The helper `_dispatch_breaker_decision(task_dir, stage, decision, *, task_id)` is called at every checkpoint. It:

- **Always** logs a `circuit_breaker_observed` event to `.dynos/<task_id>/events.jsonl` with fields: `stage`, `decision`, `would_have_aborted`, `would_have_downgraded`.
- When `BREAKER_ACTIVE is True` and `decision["abort"] is True`: calls `_apply_abort(task_dir, decision)` which writes `escalation.md`, appends to `execution-log.md`, and transitions the manifest to FAILED.
- When `BREAKER_ACTIVE is True` and `decision["action"] == "downgrade"`: additionally logs a `circuit_breaker_downgrade_suggested` event.
- When `BREAKER_ACTIVE is False`: returns after logging `circuit_breaker_observed`. No abort, no downgrade log.

Both event names are listed in `DIAGNOSTIC_ONLY_EVENTS` in `hooks/lib_log.py` — they do not participate in receipts, stage transitions, or any downstream gate.

The helper is fully exception-safe: any internal error is printed to stderr and `None` is returned.

## Activation procedure

1. After running N tasks with `BREAKER_ACTIVE=False`, review the `circuit_breaker_observed` events in per-task `events.jsonl` files (see grep pattern below).
2. Inspect `would_have_aborted` and `would_have_downgraded` fields for false positives.
3. Verify zero false positives across the review window.
4. Flip `BREAKER_ACTIVE = True` in a single-line follow-up PR.
5. Threshold calibration (adjusting `SMALL_TASK_TOKEN_LIMIT`, `BUGFIX_TOKEN_LIMIT`, spawn thresholds) is a separate task informed by the observed events and is independent of the activation flip.

Closes residual `a1c4a97c`.

## Per-task events.jsonl location

Events are written to `.dynos/<task_id>/events.jsonl` within the project root.

To inspect all circuit-breaker observed events for a specific task:

```bash
grep '"event": "circuit_breaker_observed"' .dynos/<task_id>/events.jsonl | python3 -m json.tool
```

To find all tasks where the breaker would have aborted:

```bash
grep -r '"event": "circuit_breaker_observed"' .dynos/*/events.jsonl \
  | python3 -c "import sys,json; [print(l) for l in sys.stdin if json.loads(l.split(':', 1)[1]).get('would_have_aborted')]"
```

To find downgrade suggestions across tasks:

```bash
grep -r '"event": "circuit_breaker_downgrade_suggested"' .dynos/*/events.jsonl
```

## Reference: 2026-04-30 token-overrun incident

On 2026-04-30 the orchestrator forged 7/8 ensemble auditors after context truncation. The subsequent audit found that receipts validated clean but token budget was consumed without signal. This incident motivated the circuit breaker's zero-yield arm (`opus_auditor_zero_yield`) and the two token-budget arms. The breaker ships in observe-only mode to allow operators to review a window of real task decisions before activating enforcement, avoiding false-positive aborts on legitimate high-token tasks.
