# External Solution Gate

## What the gate enforces

The external-solution gate runs at **Step 2c** of every task — before the planner invents a local solution — and decides whether external research is required. Its purpose is to prevent the planner from re-implementing something that a well-known library, pattern, or prior solution already handles better.

The gate is **always-runs**: it fires for every task type and every risk level. The decision is deterministic and owned by the gate artifact; the orchestrator must not override it in prompt logic.

After task-20260507-005 closed a bypass path, the gate additionally enforces that when `search_recommended` is `true`, the orchestrator must record verifiable research evidence (consulted URLs and a findings summary) before the spec can advance. `run-spec-ready` exits non-zero if the receipt is absent.

## Three-layer check

`run-spec-ready` applies three checks in order when the gate has `search_recommended=true`:

1. **Existence check (layer a)** — verifies `search-conducted.json` (the receipt written by `write-search-receipt`) exists. No receipt, no spec advance. This check is unchanged from the pre-fix version.

2. **Temporal cross-check (layer b)** — verifies `<task_dir>/web-tool-log.jsonl` contains at least one entry with `tool` in `{"WebSearch", "WebFetch"}` and timestamp `gate.written_at <= entry.ts <= receipt.timestamp`. The log file is written by a `PostToolUse` hook (`hooks/web_tool_log.py`) — the orchestrator cannot forge entries because the harness owns the writes. If `gate.written_at` is absent (legacy gate file from before this fix) or malformed, both this check and layer (c) are skipped with a `[GATE] external-solution-cross-check skipped: legacy gate file` log line; layer (a) still fires.

3. **Structure check (layer c)** — verifies the receipt has `urls_consulted` (list of ≥1 entries, each matching `^https?://`) and `findings_summary` (stripped length ≥200 chars). Empty / dismissive receipts cannot satisfy this layer. Same backward-compat as layer (b): skipped on legacy gate files.

## How to provide research evidence

When `search_recommended` is `true` in the gate artifact, the orchestrator must:

1. Read `query_reason` and `decision_basis` from the gate artifact to form the search query.
2. Conduct external research using `WebSearch` or `WebFetch` tool calls. The harness records these events to `web-tool-log.jsonl` automatically — they cannot be forged retroactively.
3. Call `write-search-receipt` with all required fields:

```text
python3 hooks/ctl.py write-search-receipt .dynos/task-{id} \
  --query "<the search query used>" \
  --urls-consulted "<url1>,<url2>" \
  --findings-summary "<one-sentence summary of what the research found>"
```

The `--urls-consulted` field must list the URLs actually fetched; `--findings-summary` must describe what the research found (not restate the query). Both fields are part of the trust-substrate: the harness matches declared URLs against `WebFetch` events in `web-tool-log.jsonl` and flags receipts where no matching fetch event exists.

### WebSearch / WebFetch event recording

Every `WebSearch` and `WebFetch` tool call made by the orchestrator during Step 2c is recorded as one JSONL entry in `.dynos/task-{id}/web-tool-log.jsonl` by the `PostToolUse` hook (`hooks/web_tool_log.py`). Each entry has `ts` (ISO 8601 UTC), `tool` (`"WebSearch"` or `"WebFetch"`), and either `query` (for WebSearch) or `url` (for WebFetch). `run-spec-ready` reads this log and validates that at least one entry's timestamp falls in the window `[gate.written_at, receipt.timestamp]`. This prevents the orchestrator from writing a receipt that claims research was done without any verifiable tool-use activity, because the hook is invoked by the harness post-execution and the LLM cannot inject entries.

## Trust-substrate pattern

The three-layer check above is a direct application of the trust-substrate pattern documented in the 2026-04-30 audit-chain forgery incident. In that incident, an orchestrator forged 7 of 8 ensemble auditor receipts after context truncation. The post-mortem established the following principle:

> Any claim that an action was taken must be backed by an unforgeable event trail that an independent verifier can check without trusting the orchestrator.

Applied to the external-solution gate:

- The **gate artifact** (`external-solution-gate.json`) is written by a deterministic hook, not by the orchestrator. The orchestrator can read it but cannot write it legitimately.
- The **WebSearch / WebFetch events** in `web-tool-log.jsonl` are written by the harness's `pre_tool_use.py` hook at call time, before the orchestrator can observe the result. They cannot be inserted retroactively.
- The **receipt** (`write-search-receipt`) is the orchestrator's commitment. Its `artifact_sha256` is computed over the live gate file. Mutating the gate file after the receipt is written causes the hash to drift, which `receipt_postmortem_generated` flags during the audit phase.

This three-layer design means a forged claim of "search was performed" requires simultaneously: (a) a plausible-looking `write-search-receipt` receipt, (b) at least one `WebFetch` event in `web-tool-log.jsonl` for a URL that appears in `--urls-consulted`, and (c) a `external-solution-gate.json` whose sha256 matches the receipt's `artifact_sha256`. All three must be consistent. Forging all three in-flight is the trust trapdoor the 2026-04-30 incident surfaced; subsequent harness changes made (b) write-once append-only, making the trapdoor unworkable.

## Reference

- `hooks/ctl.py` — `run-external-solution-gate`, `write-search-receipt`, `run-spec-ready` subcommands
- `skills/start/SKILL.md` Step 2c — orchestrator instructions for this gate
- `docs/system-sequence-and-loopholes.md` — broader gate sequence and known loophole catalogue
