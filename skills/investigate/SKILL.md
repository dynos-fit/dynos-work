---
name: investigate
description: "Deep bug investigation. Pass a short description of the problem — error message, unexpected behavior, or failing test. Returns structured root cause analysis with evidence and fix recommendation. Runs only when the user explicitly types /dynos-work:investigate; never auto-triggered from conversation or by reported bugs."
---

# dynos-work: investigate

Runs a deterministic-first investigation pipeline: triage first, reasoning second, citation validation third. The LLM never gathers evidence on its own — it reasons over a pre-built dossier, and that guarantee is enforced in code: the investigator role's Read access is restricted to the investigations directory and its Grep/Glob calls are denied by the pre-tool-use hook.

## Ruthlessness Standard

- Name the mechanism, not the symptom.
- Every conclusion must cite a pre-minted evidence ID from the dossier.
- If the evidence does not support a claim, say so instead of guessing.

## Phase 1 — Deterministic Evidence Gathering

Create an investigation directory inside the project's `.dynos/` (never `/tmp` — the write policy denies it), then run the triage orchestrator before invoking the LLM:

```bash
INVESTIGATION_DIR=".dynos/investigations/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$INVESTIGATION_DIR"
python3 "${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}/debug-module/triage.py" --bug "<bug text>" --repo . --out "$INVESTIGATION_DIR/dossier.json"
```

This produces an evidence dossier with pre-minted evidence IDs, git blame ranges, and co-change history. The LLM does not gather evidence — it reasons over what triage has already found, so triage puts the code the bug text names into the dossier:

- `F-NNN` — each mentioned file that exists in the repo (path, size, line count, a short numbered head), plus a numbered snippet for every `file.py:123` or `file.py:120-140` reference (and stack-frame line).
- `SY-NNN` — the definition of each identifier the bug text names (backticked, `name()`, snake_case, camelCase, dotted, UPPER_CASE constants, and `cmd_*` handlers derived from kebab-case CLI subcommands like `clear-role`): file, line range, and up to 120 numbered source lines. Looked up in the mentioned files first, then repo-wide for distinctive names.
- `MF-NNN` / `S-NNN` — linter and Semgrep silent-accomplice findings, limited to the mentioned files when any resolve (capped at 100 each otherwise).
- `T-NNN`, `ST-NNN`, `CG-NNN` — test results, stack frames, coverage gaps.

`evidence_scope` in the dossier records which files and symbols resolved, which did not, and how many out-of-scope findings were dropped.

## Phase 2 — Causal Reasoning

If a task dir exists, grant the investigator role before the spawn (enforces dossier-only reads in the hook): `python3 "${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}/hooks/ctl.py" grant-role .dynos/task-{id} --role investigator`. Standalone investigations (no task) skip this.

Spawn the `@investigator` subagent. Pass the contents of `$INVESTIGATION_DIR/dossier.json` as input and instruct the agent to:

- Trace root cause, immediate cause, and detection failure.
- Cite only evidence IDs that exist in the dossier.
- **Return the structured bug-report JSON as its final message** — a single JSON object matching `debug-module/schemas/bug_report.schema.json`, no prose, no markdown fences. The investigator is read-only (`tools: [Read, Grep]`); it must NOT attempt to write any file.

The agent must not invent file paths, symbols, or findings outside the dossier.

## Phase 3 — Validation, Persistence, and Render

Pipe the agent's returned JSON into the deterministic finalize step. It validates the report structure (every claim must carry non-empty `evidence_ids`), mechanically verifies every cited evidence ID exists in the dossier, renders the Markdown via `debug-module/lib/render_report.py` (`render_report.render`), and persists `bug_report.json` + the rendered `report.md` — rejection exits non-zero and nothing is persisted:

```bash
python3 "${CODEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}/debug-module/triage.py" finalize \
  --report - \
  --dossier "$INVESTIGATION_DIR/dossier.json" <<'REPORT_JSON'
{ ...the investigator's returned JSON, verbatim... }
REPORT_JSON
```

- Exit 0: print the rendered `report.md` content to the user.
- Exit 1 (rejected — structural violation or unknown citation): the stderr names each violation. Re-spawn the investigator ONCE with the violations appended to its prompt; if the second attempt is also rejected, report the failure honestly. Do NOT edit the report JSON yourself to make validation pass — fabricating or trimming citations defeats the entire evidence chain.

## Usage

```
/dynos-work:investigate <your problem description>
```

Examples:
```
/dynos-work:investigate TypeError: Cannot read properties of undefined reading 'id' at UserService.ts:47
/dynos-work:investigate the checkout flow always skips the discount calculation when coupon code is applied
/dynos-work:investigate test suite: AuthController > login > should return 401 on invalid password is failing
/dynos-work:investigate the GitHub Actions workflow fails on main but tests pass locally
```
