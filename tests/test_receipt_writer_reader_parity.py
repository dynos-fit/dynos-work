"""Parity test for receipt writers and readers (task-20260419-002 G3).

Acceptance criteria 6, 7, 8 of the spec. This test enforces:

  1. Every receipt writer in ``hooks/lib_receipts.py`` (any ``^def receipt_``
     function that calls ``write_receipt(task_dir, "<literal>", ...)`` with
     a literal string first-positional step_name) has at least one reader
     (``read_receipt(..., "<literal>")``) somewhere under ``hooks/``,
     ``skills/``, ``memory/``, or ``cli/assets/templates/base/`` — OR
     the step_name is explicitly allowlisted in ``_INTENTIONALLY_WRITE_ONLY``
     below with a non-empty reason string.

  2. Every literal-string ``read_receipt(..., "<literal>")`` call site has a
     matching writer in ``hooks/lib_receipts.py``. Writers may be literal
     strings OR f-string prefixes (e.g. ``f"planner-{phase}"`` supplies
     both ``planner-spec`` and ``planner-plan`` via the ``planner-``
     prefix). NO allowlist on this side — a reader-without-writer is a
     hard failure because it indicates a caller reading a step that was
     never produced.

  3. Every ``receipt: <step>`` citation inside
     ``gate_errors.append(...)`` or ``_refuse(...)`` call sites in
     ``hooks/lib_core.py`` MUST have BOTH a writer AND a reader (or
     allowlist entry for the reader-side fallback). Dynamic gate citations
     (e.g. ``f"receipt: executor-{seg_id} ..."``) surface as the captured
     prefix (``executor-``) and pass when the f-string writer/reader
     prefix covers them.

  4. Every entry in ``_INTENTIONALLY_WRITE_ONLY`` carries a non-empty
     reason string. Adding an entry therefore requires touching this file
     AND documenting why — no silent allowlist growth.

The allowlist was populated from a live repo scan at task execution time
(see ``.dynos/task-20260419-002/evidence/seg-2-evidence.md``) — every
entry corresponds to a grep-verified writer step_name that has zero
literal-string readers across the searched directories. Future receipts
added by follow-up work that are genuinely write-only must be added here
EXPLICITLY with a reason. Guessing is forbidden.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LIB_RECEIPTS = ROOT / "hooks" / "lib_receipts.py"
LIB_CORE = ROOT / "hooks" / "lib_core.py"
READER_SEARCH_DIRS = [
    ROOT / "hooks",
    ROOT / "skills",
    ROOT / "memory",
    ROOT / "cli" / "assets" / "templates" / "base",
]


# ---------------------------------------------------------------------------
# Allowlist — grep-verified write-only receipts (see evidence file).
#
# Populated from a live repo scan at 2026-04-18 (task-20260419-002, seg-2):
#   * literal writers enumerated via AST walk of hooks/lib_receipts.py
#   * literal readers enumerated via grep for
#       read_receipt(..., "<literal>") across
#       hooks/, skills/, memory/, cli/assets/templates/base/
#   * writers - readers = candidate write-only set
#   * each candidate manually confirmed to have no dynamic reader
#     reachable via prefix match (i.e. not covered by an f-string reader)
#
# Dict (not set) so each entry MUST carry a reason string. Adding a new
# entry is an explicit review touch: the CI diff will show both the
# step_name and the justification.
# ---------------------------------------------------------------------------
_INTENTIONALLY_WRITE_ONLY: dict[str, str] = {
    # Written by receipt_search_conducted (hooks/lib_receipts.py:receipt_search_conducted).
    # Consumed by validate_chain via a loop variable: the external-solution
    # gate (commit f725198 / a848461) appends "search-conducted" to the
    # `required` list at lib_receipts.py:613-615, and validate_chain
    # iterates `for receipt_name in required: read_receipt(..., receipt_name)`.
    # Literal-grep parity check cannot see the variable dispatch and reports
    # the writer as orphaned; allowlist the step with the validate_chain
    # rationale.
    "search-conducted": (
        "consumed by validate_chain via loop variable; the external-solution "
        "gate appends 'search-conducted' to validate_chain's required list "
        "(lib_receipts.py:613-615) and validate_chain reads it through "
        "`for receipt_name in required: read_receipt(...)` which the parity "
        "grep cannot detect"
    ),
    # Written by receipt_plan_routing (hooks/lib_receipts.py:receipt_plan_routing).
    # Observability-only: captures which agent the plan skill routed to so
    # the audit trail records the routing decision. No runtime gate reads
    # it directly — the validate_chain helper walks it via a variable
    # dispatch (`for receipt_name in required: read_receipt(...,
    # receipt_name)`) which literal-grep cannot see.
    "plan-routing": (
        "observability-only; written by receipt_plan_routing, no literal "
        "reader in hooks/skills/memory/cli-templates (validate_chain reads "
        "via a loop variable which the parity grep cannot detect)"
    ),
    # Written by receipt_post_completion (hooks/lib_receipts.py:receipt_post_completion).
    # End-of-pipeline observational receipt proving post-DONE handlers ran.
    # validate_chain references it through its `required` list but via a
    # loop variable, so literal grep does not see a reader. No gate reads
    # it directly; downstream dashboards inspect the receipt file on disk
    # rather than via read_receipt().
    "post-completion": (
        "written by receipt_post_completion as end-of-pipeline marker; "
        "validate_chain reads it via loop variable (invisible to literal "
        "grep); no gate or skill reads it directly"
    ),
    # Written by receipt_tdd_tests (hooks/lib_receipts.py:receipt_tdd_tests).
    # TDD_REVIEW exit gate reads `human-approval-TDD_REVIEW` for the
    # actual approval signal; the tdd-tests receipt itself is an
    # observational marker that tests were committed and tokens recorded.
    # No literal reader exists in any searched directory.
    "tdd-tests": (
        "observability-only; written by receipt_tdd_tests to record token "
        "usage and evidence path, but TDD_REVIEW exit gate reads "
        "human-approval-TDD_REVIEW not tdd-tests — no reader exists"
    ),
    # Written by receipt_scheduler_refused (hooks/lib_receipts.py:
    # receipt_scheduler_refused). Emitted from
    # scheduler.handle_receipt_written when compute_next_stage returns a
    # non-None next_stage with non-empty missing_proofs. Parallel to the
    # scheduler_transition_refused event: pure observability with no
    # runtime gate reading it. The stage transition is gated by
    # transition_task + receipt_force_override, not by this receipt.
    "scheduler-refused": (
        "observability-only; no gate reads it; parallel to "
        "scheduler_transition_refused event"
    ),
}


# ---------------------------------------------------------------------------
# Writer extraction — AST walk of hooks/lib_receipts.py
# ---------------------------------------------------------------------------


def _writer_step_info() -> tuple[set[str], set[str]]:
    """Return ``(literal_writers, fstring_writer_prefixes)``.

    Walks every ``^def receipt_`` function in ``hooks/lib_receipts.py``.
    For each ``write_receipt(task_dir, <arg>, ...)`` call found inside
    the function body:

    * If ``<arg>`` is an ``ast.Constant`` string, the literal value is
      added to ``literal_writers``.
    * If ``<arg>`` is an ``ast.JoinedStr`` (an f-string), the constant
      prefix (text before the first ``FormattedValue`` hole) is added
      to ``fstring_writer_prefixes``. A prefix like ``"executor-"``
      matches every family member ``executor-seg-1``, ``executor-seg-2``,
      etc.
    * If ``<arg>`` is any other expression (``Name``, etc.), it is
      ignored — such call sites produce step_names we cannot statically
      enumerate. This is an accepted limitation; the receipt writer
      functions wrapping them each have a stable literal or f-string
      branch that we DO capture (e.g. ``receipt_planner_spawn`` uses
      ``step_name = f"planner-{phase}"`` and then passes ``step_name``;
      we pick up the f-string prefix via the assignment walk below).

    The second pass scans ``Assign`` nodes of shape ``step_name = <f">"``
    inside the same receipt_ function so we do not miss the
    ``step_name = f"planner-{phase}"; write_receipt(task_dir, step_name,
    ...)`` idiom used in ``receipt_planner_spawn``.
    """
    src = LIB_RECEIPTS.read_text()
    tree = ast.parse(src)

    literal_writers: set[str] = set()
    fstring_writer_prefixes: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if not node.name.startswith("receipt_"):
            continue

        # Pass 1: collect Assigns of shape `step_name = <expr>` so we can
        # resolve the variable when write_receipt is called with
        # `step_name` rather than the literal.
        local_name_bindings: dict[str, ast.expr] = {}
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Name):
                        local_name_bindings[tgt.id] = sub.value

        # Pass 2: every write_receipt(task_dir, <arg>, ...) call.
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            if not isinstance(sub.func, ast.Name) or sub.func.id != "write_receipt":
                continue
            if len(sub.args) < 2:
                continue
            arg = sub.args[1]

            # If the arg is a Name, try to resolve to its assignment.
            if isinstance(arg, ast.Name) and arg.id in local_name_bindings:
                arg = local_name_bindings[arg.id]

            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                literal_writers.add(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                prefix = _fstring_constant_prefix(arg)
                if prefix:
                    fstring_writer_prefixes.add(prefix)

    return literal_writers, fstring_writer_prefixes


def _fstring_constant_prefix(joined: ast.JoinedStr) -> str:
    """Return the constant-string prefix of an f-string, up to the first
    ``FormattedValue`` hole. Returns ``""`` if the f-string starts with
    a hole.
    """
    prefix_parts: list[str] = []
    for part in joined.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            prefix_parts.append(part.value)
        else:
            break
    return "".join(prefix_parts)


def _writer_step_names() -> set[str]:
    """Public surface used by tests: literal writers only.

    The symmetric-reader-has-writer check uses the richer
    ``_writer_step_info`` to handle f-string prefixes separately.
    """
    literals, _prefixes = _writer_step_info()
    return literals


# ---------------------------------------------------------------------------
# Reader extraction — text-search for read_receipt(..., "<literal>")
# ---------------------------------------------------------------------------

# Captures the first string-literal arg of read_receipt — either a normal
# double-quoted string OR a double-quoted f-string prefix before the first
# `{`. Single-quoted forms are also accepted.
#
# Shape: read_receipt(<anything, including task_dir or other expr>, <lit>)
# We keep the regex permissive on the gap between `(` and the quote because
# call sites span multiple lines (AST would be stricter but the target is
# plain-text search — consistent with the grep-based parity the plan asks
# for).
_READ_RECEIPT_LITERAL_RE = re.compile(
    r"""read_receipt\s*\(
        [^)"']*                 # everything up to the step_name arg
        (?:"([^"{}]+)"|'([^'{}]+)')
    """,
    re.VERBOSE | re.DOTALL,
)

# f-string readers: `f"executor-{...}"` → capture the prefix up to `{`.
_READ_RECEIPT_FSTRING_RE = re.compile(
    r"""read_receipt\s*\(
        [^)"']*
        f(?:"([^"{}]*)\{|'([^'{}]*)\{)
    """,
    re.VERBOSE | re.DOTALL,
)


def _iter_search_files() -> list[Path]:
    """Enumerate every text file under the reader search dirs whose
    extension is .py or .md. Skips __pycache__, binary files.
    """
    files: list[Path] = []
    for base in READER_SEARCH_DIRS:
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if "__pycache__" in p.parts:
                continue
            if p.suffix not in {".py", ".md"}:
                continue
            files.append(p)
    return files


def _reader_step_info() -> tuple[set[str], set[str], list[tuple[Path, int, str]]]:
    """Return ``(literal_readers, fstring_reader_prefixes, citations)``.

    * ``literal_readers``: set of step_name strings that appear as the
      literal second arg of ``read_receipt(..., "<lit>")`` — with some
      permissive regex fudge for multi-line calls (see
      ``_READ_RECEIPT_LITERAL_RE``).
    * ``fstring_reader_prefixes``: constant prefixes of f-string reader
      call sites (e.g. ``executor-`` from ``f"executor-{seg_id}"``).
    * ``citations``: list of (file, line_number, step_name) triples for
      use in failure messages.
    """
    literal_readers: set[str] = set()
    fstring_reader_prefixes: set[str] = set()
    citations: list[tuple[Path, int, str]] = []

    for f in _iter_search_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in _READ_RECEIPT_LITERAL_RE.finditer(text):
            lit = m.group(1) or m.group(2)
            # Skip the literal capture if the preceding 'f' makes this an
            # f-string — the f-string regex handles those.
            start = m.start()
            # The `f` (if present) sits immediately before the opening quote.
            # Locate the quote the regex matched.
            quote_offset = m.start(1) - 1 if m.group(1) else m.start(2) - 1
            is_fstring = quote_offset > 0 and text[quote_offset - 1] == "f"
            if is_fstring:
                continue
            literal_readers.add(lit)
            line_no = text.count("\n", 0, start) + 1
            citations.append((f, line_no, lit))
        for m in _READ_RECEIPT_FSTRING_RE.finditer(text):
            prefix = m.group(1) or m.group(2) or ""
            if prefix:
                fstring_reader_prefixes.add(prefix)

    return literal_readers, fstring_reader_prefixes, citations


def _reader_step_names() -> set[str]:
    """Public surface: literal reader step_names only."""
    literals, _prefixes, _cites = _reader_step_info()
    return literals


# ---------------------------------------------------------------------------
# Gate citation extraction — `receipt: <step>` inside
# gate_errors.append(...) or _refuse(...)
# ---------------------------------------------------------------------------


_GATE_CITATION_RE = re.compile(
    r"""(?:gate_errors\.append|_refuse)\s*\(
        [^)]*?
        receipt:\s+([a-z][a-z0-9-]*)
    """,
    re.VERBOSE | re.DOTALL,
)


def _gate_citation_step_names() -> set[tuple[str, int, str]]:
    """Return set of (file, line_no, step_name) for every
    ``receipt: <step>`` citation inside
    ``gate_errors.append(...)`` or ``_refuse(...)`` in hooks/lib_core.py.

    Step_name is the regex capture; for dynamic f-string citations such
    as ``f"receipt: executor-{seg_id} ..."``, the capture is the literal
    prefix (``executor-``). Downstream coverage checks treat a trailing
    hyphen as an f-string family marker and match it against writer /
    reader f-string prefixes.
    """
    text = LIB_CORE.read_text()
    results: set[tuple[str, int, str]] = set()
    for m in _GATE_CITATION_RE.finditer(text):
        step = m.group(1)
        line_no = text.count("\n", 0, m.start()) + 1
        results.add((str(LIB_CORE), line_no, step))
    return results


# ---------------------------------------------------------------------------
# Coverage helpers
# ---------------------------------------------------------------------------


def _reader_covers(
    step: str,
    literal_readers: set[str],
    fstring_reader_prefixes: set[str],
) -> bool:
    """Does any reader (literal or f-string prefix) cover ``step``?

    Coverage semantics:
      * exact literal match, OR
      * some f-string reader prefix P where ``step.startswith(P)`` and
        ``step != P`` (family member covered by dynamic reader), OR
      * ``step`` itself ends in ``-`` (meaning ``step`` is a dynamic
        prefix extracted from an f-string citation) and some literal
        reader starts with ``step``.
    """
    if step in literal_readers:
        return True
    for prefix in fstring_reader_prefixes:
        if prefix and step != prefix and step.startswith(prefix):
            return True
    if step.endswith("-"):
        if any(lit.startswith(step) for lit in literal_readers):
            return True
        if step in fstring_reader_prefixes:
            return True
    return False


def _writer_covers(
    step: str,
    literal_writers: set[str],
    fstring_writer_prefixes: set[str],
) -> bool:
    """Does any writer (literal or f-string prefix) cover ``step``?"""
    if step in literal_writers:
        return True
    for prefix in fstring_writer_prefixes:
        if prefix and step != prefix and step.startswith(prefix):
            return True
    if step.endswith("-"):
        if any(lit.startswith(step) for lit in literal_writers):
            return True
        if step in fstring_writer_prefixes:
            return True
    return False


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_writer_has_reader_or_is_allowlisted():
    """AC 6 (writer-side): every literal ``write_receipt(task_dir,
    "<step>", ...)`` step_name in ``hooks/lib_receipts.py`` has a
    matching reader under the searched directories, OR is in
    ``_INTENTIONALLY_WRITE_ONLY`` with a non-empty reason.

    Failure names the exact step_names that have no reader and no
    allowlist entry.
    """
    literal_writers, _fstring_writers = _writer_step_info()
    literal_readers, fstring_reader_prefixes, _cites = _reader_step_info()

    # Sanity: the writer set cannot be empty — if AST extraction broke,
    # we would silently pass. Guard against that regression.
    assert literal_writers, (
        "AST extraction returned zero literal writers from "
        f"{LIB_RECEIPTS}. Either the file is empty or the AST walk is "
        "broken; either way the parity test has lost its teeth."
    )

    missing: list[str] = []
    for step in sorted(literal_writers):
        if _reader_covers(step, literal_readers, fstring_reader_prefixes):
            continue
        if step in _INTENTIONALLY_WRITE_ONLY:
            continue
        missing.append(step)

    assert not missing, (
        "writer without reader and no allowlist entry: "
        f"{missing}\n"
        "Either add a read_receipt(..., \"<step>\") call-site under "
        "hooks/, skills/, memory/, or cli/assets/templates/base/, OR add "
        "the step_name to _INTENTIONALLY_WRITE_ONLY in this test file "
        "with a documented reason. Guessing the allowlist is forbidden."
    )


def test_every_reader_has_writer():
    """AC 6 (reader-side): every literal ``read_receipt(..., "<step>")``
    call-site step_name has a matching writer in
    ``hooks/lib_receipts.py`` (literal or f-string prefix). NO ALLOWLIST
    ON THIS SIDE — a reader without a writer indicates a caller reading
    a step that never gets produced and is a hard failure.
    """
    literal_writers, fstring_writer_prefixes = _writer_step_info()
    _literal_readers, _prefixes, citations = _reader_step_info()

    assert citations, (
        "reader extraction found zero read_receipt(..., \"<lit>\") call "
        "sites. Either the search regex is broken or the entire codebase "
        "stopped reading receipts — either is a severe regression the "
        "parity test should refuse to silently pass."
    )

    missing: list[tuple[str, Path, int]] = []
    seen: set[str] = set()
    for path, line_no, step in citations:
        if step in seen:
            continue
        seen.add(step)
        if _writer_covers(step, literal_writers, fstring_writer_prefixes):
            continue
        missing.append((step, path, line_no))

    assert not missing, (
        "reader without writer: "
        + "; ".join(
            f"read_receipt(..., {s!r}) at {p}:{ln}"
            for s, p, ln in missing
        )
        + "\nAdd a matching write_receipt() call in hooks/lib_receipts.py "
        "or remove the read."
    )


def test_every_transition_gate_step_has_writer_and_reader():
    """AC 7: every ``receipt: <step>`` citation inside
    ``gate_errors.append(...)`` or ``_refuse(...)`` in
    ``hooks/lib_core.py`` must be backed by BOTH a writer AND a reader
    (or allowlist entry for the reader). A gate that cites a missing
    receipt is a liar — its error message points the operator at a
    receipt the code never produces, making the failure mode opaque.
    """
    literal_writers, fstring_writer_prefixes = _writer_step_info()
    literal_readers, fstring_reader_prefixes, _cites = _reader_step_info()
    citations = _gate_citation_step_names()

    assert citations, (
        "gate-citation extraction found zero `receipt: <step>` citations "
        "in hooks/lib_core.py. Either the regex is broken or every gate "
        "stopped citing receipts — either case is a severe regression."
    )

    writer_missing: list[tuple[str, str, int]] = []
    reader_missing: list[tuple[str, str, int]] = []
    for path, line_no, step in sorted(citations):
        if not _writer_covers(step, literal_writers, fstring_writer_prefixes):
            writer_missing.append((step, path, line_no))
        has_reader = _reader_covers(step, literal_readers, fstring_reader_prefixes)
        if not has_reader and step not in _INTENTIONALLY_WRITE_ONLY:
            # Allow the dynamic-family case: if step ends in `-`, we
            # already attempted prefix match inside _reader_covers, so
            # falling through here means genuinely no reader exists.
            reader_missing.append((step, path, line_no))

    errs: list[str] = []
    if writer_missing:
        errs.append(
            "gate cites receipt without writer: "
            + "; ".join(
                f"{step!r} at {p}:{ln}" for step, p, ln in writer_missing
            )
        )
    if reader_missing:
        errs.append(
            "gate cites receipt without reader: "
            + "; ".join(
                f"{step!r} at {p}:{ln}" for step, p, ln in reader_missing
            )
        )
    assert not errs, "\n".join(errs)


def test_allowlist_entries_have_comments():
    """AC 8: each entry in ``_INTENTIONALLY_WRITE_ONLY`` carries a
    non-empty reason string. Entries are a dict — not a set — expressly
    so reasons are mandatory. An empty-string reason (whitespace only
    counts as empty after strip) defeats the documentation purpose; we
    reject it here at test time so the review diff cannot sneak past a
    vague or missing justification.
    """
    bad: list[str] = []
    for step, reason in _INTENTIONALLY_WRITE_ONLY.items():
        if not isinstance(reason, str):
            bad.append(f"{step}: reason is not a string ({type(reason).__name__})")
            continue
        if not reason.strip():
            bad.append(f"{step}: reason is empty or whitespace-only")
            continue
        # A one-line reason is fine but it must be substantive. Guard
        # against lazy placeholders like "TODO" or "n/a".
        low = reason.strip().lower()
        if low in {"todo", "tbd", "n/a", "na", "write-only", "write only"}:
            bad.append(
                f"{step}: reason is a placeholder ({reason!r}); "
                "write a substantive explanation"
            )
    assert not bad, (
        "_INTENTIONALLY_WRITE_ONLY entries with missing / weak reasons:\n"
        + "\n".join(f"  - {b}" for b in bad)
    )
