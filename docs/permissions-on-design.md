# Design: Permissions-ON Usability Without Weakening Guardrails

Status: IMPLEMENTED in v7.4.1 (2026-06-11) — kept as the design rationale record.
See CHANGELOG.md [7.4.1] and the addendum in docs/write-boundary-spec.md.
Scope: make `start → execute → audit` runnable by a user with Claude Code permissions ON
(no `--dangerously-skip-permissions`, no blanket allow) such that the plugin never denies
an action the plugin itself prescribed — while preserving every anti-hallucination /
anti-overstep / anti-false-completion invariant.

---

## 0. Verified findings (corrections to the problem statement first)

Every problem below was confirmed against the code. Three claims needed correction, and
two new holes were found during analysis.

### Corrections

1. **"write_policy is non-deterministic" — it is deterministic, but state-dependent and
   opaque.** Two mechanisms make identical-looking commands diverge:
   - The role used for *every* orchestrator tool call is read fresh from the single
     mutable file `active-segment-role` (`hooks/pre_tool_use.py:222-244`). Stamping a
     role for a subagent silently changes the orchestrator's own write rights between
     two otherwise-identical commands.
   - Per-role allowlists differ per file, not per directory: role `planning` may write
     `spec.md` but not `execution-log.md` in the *same* task dir
     (`hooks/write_policy.py:327-330`). So "same role, same dir, one write passes one
     fails" is correct policy behavior with an unreadable error.
   Additionally the Bash pre-filter regex `>>?\s*([^\s;|&>]+)`
   (`hooks/pre_tool_use.py:74`) matches `>` *inside quoted strings*, so any prompt text
   piped via `echo "...">"` patterns trips it — the "path-like tokens in prompt
   arguments" report.

2. **"run-execution-verify-evidence is stage-gated to EXECUTION" — true, but
   transitively.** The command has no explicit gate; it calls
   `_compute_execution_batch_payload` which raises on `stage != "EXECUTION"`
   (`hooks/ctl.py:2661-2665`, called at `ctl.py:4854`). `run-execution-finish`
   advances EXECUTION → TEST_EXECUTION (`ctl.py:4813`), and the execute skill orders
   finish (SKILL.md:298, end of Step 3) *before* verify-evidence (SKILL.md:323,
   Step 5). Hard error confirmed; the fix is ordering + gate-widening, not gate removal.

3. **`stamp-role` prose contradicts itself about audit roles.**
   `skills/execute/SKILL.md:164` claims "audit-* roles cannot be stamped"; the actual
   allowlist *includes* all audit-* roles (`hooks/ctl.py:2202-2209`) and
   `skills/audit/SKILL.md:145-151` explicitly instructs stamping them. The real forgery
   defense is the spawn-log cross-check at receipt time, not the stamp allowlist.

### New holes found during analysis (must fix; both strengthen invariants)

- **H1 — executor roles may write the plugin's own code.** `decide_write` allows any
  out-of-task path for executor roles ("repo work artifact allowed",
  `hooks/write_policy.py:292-293`) with **no containment to the project root**. In a
  normal user repo the plugin lives under `~/.claude/plugins/...`; an executor (or the
  orchestrator falling back to `execute-inline`) can edit `hooks.json`,
  `write_policy.py`, or `ctl.py` there — the exact self-disable primitive the mission
  forbids. Fix in §D6.

- **H2 — Bash-level enforcement is heuristic in both directions.** Besides the false
  positives above, `python3 -c "open(p,'w')"` and helper scripts that write internally
  (`build_prompt_context.py --sidecar`) are invisible to the pre-filter. The doc and
  denial messages must stop implying the Bash filter is a guarantee; the real
  guarantees are receipts + spawn-log + ctl-internal `require_write_allowed`. We keep
  the filter as defense-in-depth and make it precise, not load-bearing.

---

## 1. Problem → root cause → change (index)

| # | Problem | Root cause (file:line) | Change |
|---|---------|------------------------|--------|
| P0-a | Policy denies skill-prescribed `/tmp`, `/dev/null`, PYTHONPATH steps | `skills/start/SKILL.md:93,266,506,510,514`; `write_policy.py:296-298` | D1 (stdin payloads), D2 (scratch dir), D4 (prose rewrite) |
| P0-b | Prompt-string `>` tokens flagged as writes | `pre_tool_use.py:72-90` regex over raw string | D5 (quote-aware tokenizer) |
| P0-c | "Non-deterministic" denials | global role file mutation + per-file allowlists (`pre_tool_use.py:222-244`, `write_policy.py:327-330`) | D3 (per-actor roles), D5 (self-explaining denials) |
| P0-d | `rm active-segment-role` prescribed but denied | `skills/start/SKILL.md:715-718`, `skills/execute/SKILL.md:232-236` vs `_WRAPPER_REQUIRED` (`write_policy.py:101`) + rm pattern (`pre_tool_use.py:87`) | D3 (`ctl clear-role`) |
| P0-e | verify-evidence after finish hard-errors | `ctl.py:2664` via `ctl.py:4854`; ordering in `skills/execute/SKILL.md:298,323` | D7-1 |
| P0-f | Hook denials masquerade as Claude permission problems; no override possible by design | `pre_tool_use.py:368,412` bare `write-policy: <reason>` | D5 (labeled denials), D4 (one allowlistable surface) |
| P1-a | Orchestrator inherits last-stamped subagent role; races under parallel auditors | single `active-segment-role`; role read per tool call (`pre_tool_use.py:226-228`); race admitted at `skills/audit/SKILL.md:153` | D3 |
| P1-b | Auditors can't verify execution ACs (correct refusal, missing proof) | no captured-command-output artifact; `spec-completion-auditor` tools `[Read, Write]` | D6 (verification-evidence runner) |
| P2-a | `files_expected` can't be a directory | exact-string membership `ctl.py:2527` | D7-2 |
| P2-b | investigator: read-only agent told to write `/tmp/bug_report.json`; dossier-only guarantee unenforced; renderer drift | `skills/investigate/SKILL.md:32` vs `agents/investigator.md:4`; `read_policy.py:57` exempts non-audit roles; `render_report.py:118,149-160` | D7-3 |
| P2-c | Enum/validator papercuts (`ci` domain, `backend` vs `backend-executor`, Reference-Code backticks) | `lib_validate.py:329-330,571-573,498-504` | D7-4 |
| — | H1 plugin-dir self-edit hole | `write_policy.py:292-293` | D6-hardening |

---

## 2. Design

### D1 — ctl wrappers accept payloads on stdin (`--from -`)

**Change.** `write-classification`, `write-execution-graph`, `write-repair-log` (and the
new commands below) accept `--from -` to read the JSON payload from stdin
(`_read_json_input`, `ctl.py:280-284`, gains a `-` branch). Skill prose switches to:

```bash
"${DYNOS_CTL}" write-classification .dynos/task-{id} --from - <<'JSON'
{ ...payload returned by the planner... }
JSON
```

Heredocs contain no redirection operator, so the (fixed, D5) pre-filter sees no write
destination; the actual file write happens inside ctl under `role=ctl` with the
capability key — exactly as today.

**Invariant preserved.** Identical schema validation, normalization, atomic write, and
event emission as the `--from <file>` path (`ctl.py` write-* commands are unchanged
except input source). No new write primitive exists: ctl could already write these
files. The agent never gains a raw filesystem write.

**UX before/after.** Before: agent writes `/tmp/classification-{id}.json` → denied →
retries → user fights prompts. After: one approved ctl call.

**Test.** `tests/test_ctl.py::test_write_classification_stdin` (payload via stdin ==
payload via file, byte-identical artifact + receipt); negative: malformed stdin JSON
exits non-zero, writes nothing.

### D2 — In-boundary scratch dir `.dynos/task-{id}/_scratch/`

**Change.** `decide_write` gains an early rule: `rel_posix.startswith("_scratch/")` →
allow create/modify/delete for every *recognized actor role* (planning, executors,
orchestrator (D3), repair-coordinator, audit-*). `_scratch/` is never consulted by any
receipt, gate, or validator — that is asserted by a test, making it forge-irrelevant by
construction. All skill prose that needs a temp file (large payloads where heredoc is
awkward, sidecar staging) uses `_scratch/`. `/tmp`, `/dev/null` and plugin-dir paths
become forbidden tokens in skill prose (contract test, §3). `/dev/null` specifically is
added to the policy as an always-allowed sink (it is not persistence; denying it only
creates noise).

**Invariant preserved.** Control-plane files keep their exact-path rules — scratch is a
disjoint namespace inside the task boundary. Nothing reads scratch as proof, so writing
it can't certify anything. Audit-* roles writing scratch cannot touch
`audit-reports/` rules (still `audit-*`-only) nor receipts (still receipt-writer-only).

**UX.** Sanctioned temp space; no more "path escapes task boundary" for staging files.

**Test.** Policy unit tests per role × scratch op; grep-test that no gate/validator/
receipt reads from `_scratch/`.

### D3 — Per-actor role resolution (replaces the single global role file)

This is the structural fix for P0-c and P1-a.

**Mechanism.**
1. **Pin the orchestrator.** The existing `session-start` hook records the main
   session's `session_id` (already present in hook payloads; cf.
   `agent_spawn_log.py:130`) to `.dynos/orchestrator-session.json` (hook-owned, like
   `spawn-log.jsonl` — agent writes denied by policy). SessionStart fires only for the
   main session (matcher `startup|clear|compact`), so a subagent can never re-pin it.
2. **New role `orchestrator`.** `pre_tool_use` resolves any tool call whose
   `session_id` matches the pin to role `orchestrator`, *never* the role file.
   `decide_write` for `orchestrator`: allow `_scratch/**`, `execution-log.md`,
   `escalation.md`, `audit-context.md`, `raw-input.md`, `discovery-notes.md`,
   `design-decisions.md` (the Q&A appends are orchestrator work in start Steps 1–2);
   deny `spec.md`/`plan.md`/`audit-reports/`/`evidence/`/all control-plane. Repo-source
   writes for inline fast-track execution are allowed **only** when
   `manifest.stage == EXECUTION` and `manifest.fast_track == true` (deterministic state
   set exclusively by ctl gates — capability follows the state machine, not
   self-declaration).
3. **Grants instead of a mutable global.** `stamp-role` becomes `grant-role`
   (back-compat alias kept): it appends a pending grant
   `{role, granted_at, ttl, consumed: false}` to `role-grants.json` (wrapper-required,
   same allowlist as today, `ctl.py:2202-2236`). When `pre_tool_use` sees a session_id
   that is neither the orchestrator pin nor already bound, it binds that session to the
   oldest unconsumed grant (hook-subprocess direct I/O, same precedent as the
   `audit-grep-quota.json` writer) and marks it consumed. Subsequent calls from that
   session resolve the bound role — mid-task re-stamps can no longer mutate a running
   subagent's role, and parallel auditors each consume their own grant.
4. **`ctl clear-role`** replaces the prescribed (and denied) `rm` — it expires
   unconsumed grants. Clearing only ever *reduces* privilege.

**Verification step (phase 0, before building).** Empirically confirm subagent hook
payloads carry a distinct `session_id` (instrumented log on a scratch task). If they do
not, key on `transcript_path` (also harness-provided) instead. If *neither*
distinguishes actors, fall back to a reduced design: keep the role file for subagents
but exclude the orchestrator by D3-1/2 — that alone fixes P0-c and the worst of P1-a.

**Update — 7.5.1 (orchestrator role adoption; PR #212).** The phase-0 verification
above resolved to the worst case: **Claude Code subagents carry neither a distinct
`session_id` nor a distinct `transcript_path`** (claude-code #7881; no separate
subagent transcript file). The documented fallback ("keep the role file for subagents
but exclude the orchestrator") does not help, because the subagent *is* the
orchestrator session — D3-2's "*never* the role file" rule therefore made every
planner/executor/auditor subagent resolve as `orchestrator` and denied all of its
role-scoped writes (design-doc/spec/plan, repo source, `evidence/`, `audit-reports/`).
The pipeline only completed in fast-track inline mode.

The shipped resolution amends D3-2 with a `subagent_isolation` capability flag
(`.dynos/config/policy.json`, default **false**):
- **`false` (Claude Code default):** the pinned orchestrator session *adopts* the
  stamped `active-segment-role` (validated against the executor/planning/audit
  allowlist; an unset or non-allowlisted role file still resolves to `orchestrator`),
  so the `ctl stamp-role` calls already issued before every spawn become effective.
- **`true`:** strict D3 is preserved — the orchestrator never adopts a stamped role
  (for a future harness that genuinely isolates subagent sessions).

The self-elevation analysis still holds: `control-plane.json` and the
orchestrator-session pin are denied to all roles regardless, and
`receipt_audit_done`'s spawn-log cross-check still rejects any `audit-reports/` write
not backed by a real receipted spawn — adoption moves provenance enforcement to
receipt time, it does not weaken it. Implementation:
`hooks/pre_tool_use.py` (`_subagent_isolation`, `_adoptable_stamped_role`).

**Self-elevation analysis.**
- Roles still come only from the ctl allowlist; sessions never name their own role.
- The orchestrator cannot consume a grant (its session is pinned first, at startup).
- A subagent cannot re-pin the orchestrator file (hook-owned + SessionStart-only).
- Residual risk: two *concurrently spawned* subagents could swap grants (bind order is
  arrival order). Spawn batches are same-class today (executor batch / auditor batch),
  write-scope classes are identical within a batch, and `receipt_audit_done`'s
  spawn-log cross-check still detects any report not backed by a real spawn. Documented
  as a known bound; mitigation (grant carries expected `subagent_type`, bind prefers a
  type match using the spawn-log `pre` entry) is included in the implementation.

**UX.** The orchestrator stops getting "stuck on planning," stops tripping read-policy
after stamping audit roles (`pre_tool_use.py:420-451` no longer applies to it), and its
log appends stop failing depending on stamp history.

**Tests.** Orchestrator session denied `audit-reports/` write even after granting
`audit-spec-completion` (the self-elevation regression test); unknown session with no
pending grant → deny; grant of non-allowlisted role refused; parallel-grant binding;
TTL expiry; `clear-role` cannot mint anything.

### D4 — One command surface + consolidated per-skill entrypoints

**Change.**
1. **Single funnel.** Extend the existing `bin/dynos` CLI with `dynos ctl ...`
   (delegating to `hooks/ctl.py`, PYTHONPATH handled inside — it already does this for
   other subcommands). Every skill-prescribed command becomes
   `"${DYNOS_CTL}" <subcommand> ...` where `DYNOS_CTL="${CLAUDE_PLUGIN_ROOT}/bin/dynos ctl"`.
   This removes `PYTHONPATH=` prefixes and the broken repo-relative
   `python3 hooks/ctl.py` form (which only works when cwd is the plugin repo itself)
   from prose, and gives the permissions-ON user **one** stable prefix to allow:
   `Bash(${CLAUDE_PLUGIN_ROOT}/bin/dynos *)` — documented in README/INTERNALS, never
   auto-written into the user's settings.
2. **Absorb bookkeeping into existing run-* commands** rather than one mega
   "run-skill" (which would hide the human gates):
   - `run-start-init` (new): mkdir, `raw-input.md` from stdin, initial `manifest.json`,
     `execution-log.md`, registry/daemon registration — replaces start Step 0's six
     hand-run actions (manifest creation moves from agent-write to ctl-write, which is
     *stricter* than today).
   - `planner-receipt` / `audit-receipt` / `run-execution-segment-done` each gain
     `--record-tokens` behavior so the separate `lib_tokens.py record` calls disappear
     from prose (the SubagentStop hook already covers spawn tokens; deterministic-step
     records move inside the ctl step that performed the deterministic work).
   - `inject-prompt` / `audit-inject-prompt` keep their current contract; prose keeps
     them as discrete steps because their stdout *is* the spawn prompt.
   - `run-execution-finish` internally runs verify-evidence (D7-1) so Step 5 becomes
     read-only confirmation.
   Net effect per skill: the orchestrator's Bash usage collapses to ~6–10 `dynos ctl`
   calls + Agent spawns + `AskUserQuestion` gates.

**Invariant preserved.** Human gates (`approve-stage` at SPEC_REVIEW / PLAN_REVIEW /
TDD_REVIEW) remain separate, explicit, hash-checked commands — consolidation never
crosses a gate. Receipts, sidecar proof-of-injection, and spawn-log cross-checks are
*more* reliably written (deterministic code instead of prose discipline). No bypass
flag exists anywhere in this design.

**UX before/after.** Before: ~30 hand-run commands per skill, each a permission prompt,
several denied by the plugin itself. After: approve one command prefix once (or click
through a handful of uniform `dynos ctl` prompts) + answer the spec/plan gates.

**Tests.** Golden-path integration test running start→execute→audit on a fixture repo
with the hook active and `DYNOS_ROLE` unset, asserting zero `write_policy_denied`
events for prescribed steps (this is the Definition-of-Done test).

### D5 — Deterministic, self-explaining enforcement

**Change.**
1. **Quote-aware extraction.** Replace `_BASH_WRITE_PATTERNS` raw-string regexes with
   `shlex`-based tokenization (`punctuation_chars=True`): redirection operators and
   `tee/mv/cp/rm/...` argv positions are detected on *tokens outside quotes*. Quoted
   prompt text, heredoc bodies, and `python -c` strings can no longer produce
   destinations. (H2 note: this filter remains defense-in-depth; under-detection via
   interpreter-internal writes is unchanged and is covered by the receipts/spawn-log
   layer — stated explicitly in `docs/write-boundary-spec.md`.)
2. **Denial message contract.** Every deny prints:
   `write-policy[dynos-work guardrail — not a Claude Code permission; changing
   permission settings will not affect this]: role=<role> path=<path> — <reason>.
   Sanctioned path: <ctl command | _scratch/ | n/a>. See docs/write-boundary-spec.md.`
   `WriteDecision` already carries `wrapper_command`; extend `allowed_globs_for_role`
   reuse so the hint names what the current role *may* do.
3. **H1 hardening.** New deny rule (all agent-sourced roles, including executors):
   paths under `CLAUDE_PLUGIN_ROOT`, the hooks.json host dir, and `~/.claude/plugins/`
   are denied unless the *project root being worked on* is the plugin repo itself
   (developer mode, detected by `.claude-plugin/` at project root — and even then,
   `hooks.json` + `hooks/write_policy.py` + `hooks/pre_tool_use.py` + `hooks/ctl.py`
   require… nothing new: in developer mode they are ordinary repo files reviewed by the
   human like any diff; outside developer mode they are absolutely denied). This closes
   the "agent edits its own guardrails" hole for every installed-plugin user.

**Invariant preserved.** Strictly tightens enforcement: fewer false positives, no new
allows except `/dev/null` (a sink) and `_scratch/` (D2). Self-explaining text changes
no decision outcomes.

**Tests.** Table-driven extractor tests (quoted `>`, heredocs, `python -c`, real
redirects, `rm` flags); message-format snapshot test; H1 tests (executor write to
plugin dir denied; same path allowed in developer-mode fixture).

### D6 — Verification-evidence runner (closes P1-b, strengthens anti-trust-me-bro)

**Change.**
1. `execution-graph.json` segments gain optional `verify_commands:
   [{id, command, criteria_ids}]`, validated at `write-execution-graph` time and locked
   by the existing plan-approval hash — i.e., **the human approves the verification
   commands at the plan gate**, the same trust anchor as the code itself.
2. New `dynos ctl run-verification-evidence .dynos/task-{id} [--segment seg]`:
   executes each declared command (cwd = project root, no LLM-supplied arguments at run
   time — the command string comes only from the approved, wrapper-written graph),
   captures `{command, exit_code, stdout_tail(8KB), stderr_tail(8KB), duration_ms}`
   into `evidence/verification/{seg-id}.json` written under `role=ctl`, and writes a
   receipt embedding the artifact's sha256.
3. `run-execution-finish` requires verification evidence for every segment that
   declares `verify_commands` (alongside D7-1's evidence checks).
4. `spec-completion-auditor` prose: for ACs mapped (via `criteria_ids`) to a
   verification record, the auditor cites the captured exit code/output as evidence —
   it still **never** accepts an executor's narrative claim; it now has machine-captured
   proof to read. ACs with no verification record and no static evidence remain
   BLOCKING, exactly as the AC9 incident correctly behaved.

**Invariant preserved / strengthened.** Auditors still certify only verifiable
evidence; the evidence is produced by deterministic code, hash-chained by a receipt,
and the commands themselves are human-approved plan content. An executor cannot doctor
the artifact (`evidence/verification/` will be ctl-owned in `decide_write`, unlike the
executor-owned `evidence/*.md`). Forging would require forging a receipt — already
rejected by the chain validator.

**UX.** "ruff check src exits 0"-style ACs stop producing false BLOCKING findings and
stop requiring a human to re-run commands manually.

**Tests.** Runner captures pass/fail faithfully (fixture commands); executor-role write
to `evidence/verification/` denied; finish blocks on missing verification; auditor
contract test that a verification-backed AC is citable; receipt-hash mismatch detected
by `validate-task-receipt-chain`.

### D7 — Targeted fixes

1. **Execute ordering.** Swap Steps: verify-evidence runs at the end of Step 3 (before
   `run-execution-finish`); `run-execution-finish` calls the same verification
   internally so the gate is one deterministic unit; `run-execution-verify-evidence`
   gains `--allow-stages EXECUTION,TEST_EXECUTION` semantics by giving
   `_compute_execution_batch_payload` an `expected_stages` parameter (callers preserve
   today's behavior; only verify-evidence widens). Prose Step 5 becomes "re-run for
   confirmation; it is now legal post-transition."
   *Invariant:* stage machine unchanged; the gate gets stronger (finish can no longer
   succeed without evidence).
2. **files_expected directories/globs.** In `_verify_git_diff_covers_files`
   (`ctl.py:2462-2527`): entry ending `/` → prefix match against covered paths; entry
   containing `*?[` → `fnmatch` against covered paths; plain entries unchanged. Mirror
   the same matcher in verify-evidence's existence check and in the cross-segment
   overlap validator (`lib_validate.py` graph rule 4: a directory entry overlaps a file
   entry it prefixes — overlap detection becomes *stricter*, not looser).
   *Invariant:* membership proof still derived from `git diff` + untracked listing;
   a glob that matches nothing in the diff still fails the segment.
3. **Investigate skill.** Phase 2 rewritten: the investigator (unchanged tools
   `[Read, Grep]`) *returns* the bug-report JSON as its final message; the orchestrator
   pipes it to a new deterministic step `triage.py finalize --from -` which (a)
   validates against `bug_report.schema.json`, (b) runs the citation check against the
   dossier's `evidence_index`, (c) persists report + rendered markdown to
   `.dynos/investigations/{id}/`. Dossier-only guarantee becomes enforced:
   `read_policy` gains an `investigator` branch (granted via D3) allowing Read of the
   dossier path only and denying Grep — the prose guarantee at
   `skills/investigate/SKILL.md:8` becomes code. `render_report.py` falls back from
   `dossier.bug_type` to `bug_report.bug_type`; the classifier taxonomy gains
   `ci-failure` and `config/process` patterns (P2, isolated change in
   `bug_classifier.py`).
   *Invariant:* citation validation is unchanged and now unskippable (it sits inside
   the only persistence path).
4. **Validator papercuts.** All enum errors adopt one helper:
   `"<field> invalid: 'ci' — valid: [backend, db, docs, infra, ml, refactor, security,
   testing, ui, migration]; did you mean 'infra'?"` (difflib close-matches), applied to
   `lib_validate.py:329-330` (domains), `571-573` (executors, with explicit
   `'backend' → 'backend-executor'` suffix hint), `761-762` (repair executors), and
   `stamp-role`/`grant-role` refusals. Reference-Code check (`lib_validate.py:498-504`):
   only backtick tokens containing `/` are treated as paths; a `### Files to be
   created` (or `to-be-created`) *section* exempts every path listed in it, in addition
   to the current per-line marker. Decide separately whether `ci` joins
   `VALID_DOMAINS` (recommendation: **no** — map to `infra` via the did-you-mean hint;
   adding a domain fans out into router/audit-plan domain tables).
   *Invariant:* validators reject the same payload set or a strict superset of hints;
   nothing formerly invalid becomes valid except non-path backtick tokens that were
   false positives.
5. **Prose/agent conformance.** Fix `skills/execute/SKILL.md:164` (stamp-role claim);
   remove both "deletion is permitted" passages (start:715, execute:232) in favor of
   `dynos ctl clear-role`; repair-coordinator's `/tmp` + self-run-ctl instruction
   (`agents/repair-coordinator.md:44`, it has neither Write nor Bash) becomes
   "return the payload; the orchestrator persists via
   `dynos ctl write-repair-log --from -`" — matching how `run-repair-log-build`
   already works; `security-auditor.md` description stops claiming "Read-only" while
   prescribing a Bash-heredoc report write (the write is sanctioned; the label is
   wrong).

---

## 3. The drift-proofing contract test

New `tests/test_skill_prose_policy_contract.py`:

1. Extract every fenced `bash`/`text` command block from every `skills/*/SKILL.md` and
   `agents/*.md`.
2. Run each through the *production* destination extractor (D5's tokenizer — same
   code, imported, not a copy) and feed every detected destination to `decide_write`
   under the role context the doc prescribes at that point (role annotations carried in
   an adjacent fixture map per skill step; the test fails if a new write appears with
   no annotation).
3. Assert: zero denials for prescribed steps.
4. Forbidden-token scan over the same blocks: `/tmp/`, `2>/dev/null` (and any
   `/dev/null` outside ctl-internal code), `PYTHONPATH=`, `rm` targeting
   `active-segment-role` or any `_CONTROL_PLANE_EXACT` name, and bare
   `python3 hooks/` invocations (must use `${DYNOS_CTL}`).
5. Agent-conformance check: any `agents/*.md` whose prose instructs a file write must
   declare a tool capable of it (Write, Edit, or Bash), and the target must be allowed
   for that agent's runtime role.

This test is what keeps prose↔policy from drifting again; it would have caught every
P0 in this document.

## 4. Invariants — proof obligations (Definition of Done §2)

Each shipped with a test (existing tests noted where they already cover it):

| Invariant | Why it still holds | Test |
|---|---|---|
| No stage advance without proof | All transitions still flow through `transition_task` + receipts; D4 moves *more* steps into ctl; D6/D7-1 add evidence preconditions to finish | existing `test_ctl*`, new finish-requires-verification |
| No forged receipts/reports | receipt-writer capability key + spawn-log cross-check untouched; `_scratch/` proven receipt-irrelevant; verification evidence is ctl-owned + hash-receipted | `test_write_policy_capability_key.py`, new D6 tests |
| No self-elevation | grants from ctl allowlist only; orchestrator session pinned at SessionStart; role file no longer orchestrator-readable-as-self; H1 closes plugin-dir edits; `hooks.json`/policy files unwritable outside developer mode | new D3/D5 suites incl. the orchestrator-grants-audit-role regression |
| Human gates at spec/plan | `approve-stage` untouched, never consolidated into a run-* command | existing `test_ctl_approve_stage.py` |
| Auditors never certify unverified claims | spec-completion auditor still blocks unevidenced ACs; D6 only adds machine-captured proof for it to read | new auditor-contract test |
| Out-of-scope writes rejected | role allowlists narrowed (orchestrator role is a subset of today's `execute-inline` fallback) | new role-matrix tests |

## 5. Implementation phases

- **Phase 0 (probe, ~0.5d):** instrument hook payloads on a scratch task; confirm
  per-actor `session_id`/`transcript_path` distinctness → finalize D3 keying.
- **Phase 1 (P0 unblock):** D5 tokenizer + denial messages, D1 stdin payloads, D2
  scratch dir, D7-1 ordering fix, D7-5 prose fixes, contract test (§3). After this
  phase a permissions-ON run already completes without plugin-self-denials.
- **Phase 2 (structure):** D3 per-actor roles + `clear-role`; D4 `dynos ctl` funnel +
  `run-start-init` + bookkeeping absorption; H1 hardening (D5-3).
- **Phase 3 (proof):** D6 verification-evidence runner + auditor wiring.
- **Phase 4 (P2 papercuts):** D7-2 globs, D7-3 investigate, D7-4 validator messages.

Each phase lands with its tests; `tests/test_skill_prose_regressions.py` and
`test_skill_stage_references.py` are extended rather than replaced.

## 6. UX summary (before → after)

- Before: orchestrator hand-runs ~30 commands per skill; ~a dozen are denied by the
  plugin's own hook with messages indistinguishable from Claude permission failures;
  user toggles permissions to no effect; pipeline dead-ends at execute Step 5 and at
  AC-verification audit findings.
- After: user allowlists (or click-approves) one `dynos ctl` prefix; the session pauses
  only at discovery questions, spec approval, plan approval, TDD approval; every denial
  that *does* appear names itself as a guardrail, names the role and path, and names
  the sanctioned alternative — and never fires on an action the skills prescribe.
