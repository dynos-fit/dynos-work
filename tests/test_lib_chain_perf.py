"""Performance-optimization regression tests for lib_chain (task-20260504-004).

Three perf changes are guarded by this file:

  perf-001 (AC 3, AC 6 partial):
      `_append_entry` MUST NOT call `os.fsync` inside the
      `with chain_path.open(...)` lock-holding block. The fsync moves to
      `_fsync_chain(chain_path)` AFTER the with-block exits.

  perf-002 (AC 6):
      `cmd_run_task_receipt_chain` for-loop body MUST contain zero
      `os.fsync` calls. Exactly one `_fsync_chain(chain_path)` call must
      appear AFTER the for-loop and BEFORE `LOCK_UN`, inside the same
      try-block (so the `finally` still releases the lock).

  perf-003 (AC 10, AC 11, AC 12):
      `_read_tail_prev_sha` reverse-seek implementation must produce
      output byte-identical to the original full-read implementation
      across chain lengths {1, 5, 100, 1000} AND the documented edge
      cases (empty file, missing trailing newline, present trailing
      newline, malformed JSON last line, multiple trailing newlines).

The forensics invariant test (AC 12) is covered by the 100-entry
parametric run plus an end-to-end `validate_chain` round-trip.

These tests use AST-walks against the live source files so they fire
if a future refactor accidentally re-introduces fsync inside a lock.
"""

from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
LIB_CHAIN_PATH = HOOKS_DIR / "lib_chain.py"
CTL_PATH = HOOKS_DIR / "ctl.py"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))


# ---------------------------------------------------------------------------
# Module-level AST cache: parse each source file once.
# ---------------------------------------------------------------------------


def _load_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def lib_chain_tree() -> ast.Module:
    return _load_tree(LIB_CHAIN_PATH)


@pytest.fixture(scope="module")
def ctl_tree() -> ast.Module:
    return _load_tree(CTL_PATH)


def _find_function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in module")


def _is_call_to(node: ast.AST, *, attr: str | None = None, name: str | None = None) -> bool:
    """True iff `node` is a Call whose func is an Attribute matching
    `<owner>.attr` OR a bare Name matching `name`.
    """
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if attr is not None and isinstance(func, ast.Attribute):
        return func.attr == attr
    if name is not None and isinstance(func, ast.Name):
        return func.id == name
    return False


def _walk_calls(node: ast.AST):
    """Yield every Call node within the subtree rooted at `node`."""
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            yield child


# ---------------------------------------------------------------------------
# perf-001: AST-structural test for _append_entry
# ---------------------------------------------------------------------------


class TestPerf001AppendEntryStructure:
    """AC 3: _append_entry must release the lock BEFORE fsync. The fsync
    is performed via _fsync_chain(chain_path), called outside the
    `with chain_path.open(...)` block."""

    def test_append_entry_function_exists(self, lib_chain_tree):
        fn = _find_function(lib_chain_tree, "_append_entry")
        assert isinstance(fn, ast.FunctionDef)

    def test_no_fsync_inside_with_block(self, lib_chain_tree):
        """No `os.fsync(...)` call appears inside the `with chain_path.open(...)`
        block of `_append_entry`."""
        fn = _find_function(lib_chain_tree, "_append_entry")

        # Locate the With node whose context manager contains chain_path.open(...)
        with_nodes = [
            stmt for stmt in ast.walk(fn) if isinstance(stmt, ast.With)
        ]
        assert with_nodes, "_append_entry must contain a `with` block"

        target_with = None
        for w in with_nodes:
            for item in w.items:
                ctx = item.context_expr
                # match `chain_path.open(...)` or any X.open(...) call
                if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute):
                    if ctx.func.attr == "open":
                        target_with = w
                        break
            if target_with is not None:
                break
        assert target_with is not None, (
            "_append_entry must have a `with <path>.open(...)` block"
        )

        # Walk the body of the with-block and assert no os.fsync call.
        for stmt in target_with.body:
            for call in _walk_calls(stmt):
                # Reject `os.fsync(...)` (Attribute form) and bare `fsync(...)` (Name form)
                if _is_call_to(call, attr="fsync"):
                    pytest.fail(
                        "perf-001 violation: os.fsync call found inside "
                        f"_append_entry's `with` block at line {call.lineno}"
                    )
                if _is_call_to(call, name="fsync"):
                    pytest.fail(
                        "perf-001 violation: bare fsync call found inside "
                        f"_append_entry's `with` block at line {call.lineno}"
                    )

    def test_fsync_chain_called_after_with_block(self, lib_chain_tree):
        """Exactly one `_fsync_chain(chain_path)` call appears in
        `_append_entry` AFTER the `with chain_path.open(...)` block.
        """
        fn = _find_function(lib_chain_tree, "_append_entry")

        # Find the `with` statement and record its end line.
        with_node: ast.With | None = None
        for stmt in fn.body:
            if isinstance(stmt, ast.With):
                with_node = stmt
                break
        assert with_node is not None, "_append_entry top-level must have a With"
        with_end = with_node.end_lineno

        # Count _fsync_chain calls AFTER the with-block.
        post_lock_fsync_calls: list[ast.Call] = []
        for stmt in fn.body:
            if stmt is with_node:
                continue
            if stmt.lineno <= with_end:
                continue
            for call in _walk_calls(stmt):
                if _is_call_to(call, name="_fsync_chain"):
                    post_lock_fsync_calls.append(call)

        assert len(post_lock_fsync_calls) == 1, (
            f"_append_entry must call _fsync_chain exactly once after the "
            f"with-block; found {len(post_lock_fsync_calls)}"
        )

    def test_no_fsync_chain_inside_with_block(self, lib_chain_tree):
        """Defensive: `_fsync_chain` must NOT be called from inside the
        with-block either (would re-introduce the same lock-hold latency)."""
        fn = _find_function(lib_chain_tree, "_append_entry")
        with_node: ast.With | None = None
        for stmt in fn.body:
            if isinstance(stmt, ast.With):
                with_node = stmt
                break
        assert with_node is not None

        for stmt in with_node.body:
            for call in _walk_calls(stmt):
                if _is_call_to(call, name="_fsync_chain"):
                    pytest.fail(
                        "_fsync_chain must not be invoked inside the "
                        f"with-block of _append_entry (line {call.lineno})"
                    )


# ---------------------------------------------------------------------------
# perf-002: AST-structural test for cmd_run_task_receipt_chain
# ---------------------------------------------------------------------------


class TestPerf002CmdRunChainStructure:
    """AC 6: `cmd_run_task_receipt_chain` for-loop body contains zero
    fsync calls; exactly one `_fsync_chain(chain_path)` call appears
    after the loop and before LOCK_UN, all inside the same try-block."""

    def test_cmd_function_exists(self, ctl_tree):
        fn = _find_function(ctl_tree, "cmd_run_task_receipt_chain")
        assert isinstance(fn, ast.FunctionDef)

    def _find_pending_for_loop(self, fn: ast.FunctionDef) -> ast.For:
        """Locate the for-loop that iterates over `pending`."""
        for node in ast.walk(fn):
            if not isinstance(node, ast.For):
                continue
            it = node.iter
            # `for kind, step, fp in pending:` — iter is Name('pending')
            if isinstance(it, ast.Name) and it.id == "pending":
                return node
        raise AssertionError("for-loop over `pending` not found in cmd_run_task_receipt_chain")

    def test_for_loop_body_has_no_fsync(self, ctl_tree):
        fn = _find_function(ctl_tree, "cmd_run_task_receipt_chain")
        loop = self._find_pending_for_loop(fn)
        for stmt in loop.body:
            for call in _walk_calls(stmt):
                if _is_call_to(call, attr="fsync"):
                    pytest.fail(
                        "perf-002 violation: os.fsync call found inside "
                        f"the for-loop over pending at line {call.lineno}"
                    )
                if _is_call_to(call, name="fsync"):
                    pytest.fail(
                        "perf-002 violation: bare fsync call found inside "
                        f"the for-loop over pending at line {call.lineno}"
                    )
                # Also: no _fsync_chain inside the per-entry loop body
                if _is_call_to(call, name="_fsync_chain"):
                    pytest.fail(
                        "perf-002 violation: _fsync_chain called inside "
                        f"the for-loop over pending at line {call.lineno}"
                    )

    def test_exactly_one_fsync_chain_after_loop_in_try(self, ctl_tree):
        """Exactly one `_fsync_chain(chain_path)` call appears AFTER the
        pending for-loop and BEFORE LOCK_UN, inside the same try-block."""
        fn = _find_function(ctl_tree, "cmd_run_task_receipt_chain")
        loop = self._find_pending_for_loop(fn)

        # Find the enclosing Try whose body contains the loop.
        enclosing_try: ast.Try | None = None
        for node in ast.walk(fn):
            if isinstance(node, ast.Try):
                # Is `loop` somewhere in this Try's body (transitively)?
                for descendant in ast.walk(node):
                    if descendant is loop:
                        enclosing_try = node
                        break
                if enclosing_try is not None:
                    break
        assert enclosing_try is not None, (
            "for-loop over pending must be inside a try-block"
        )

        # Inside enclosing_try.body, find statements AFTER the loop.
        loop_end = loop.end_lineno
        post_loop_fsync_chain_calls: list[ast.Call] = []
        for stmt in enclosing_try.body:
            if stmt.lineno <= loop_end:
                continue
            for call in _walk_calls(stmt):
                if _is_call_to(call, name="_fsync_chain"):
                    post_loop_fsync_chain_calls.append(call)

        assert len(post_loop_fsync_chain_calls) == 1, (
            f"perf-002 violation: expected exactly 1 _fsync_chain call "
            f"after the for-loop inside the same try-block; "
            f"got {len(post_loop_fsync_chain_calls)}"
        )

        # And: no os.fsync after the loop in the try-body either (must
        # only go through _fsync_chain).
        for stmt in enclosing_try.body:
            if stmt.lineno <= loop_end:
                continue
            for call in _walk_calls(stmt):
                if _is_call_to(call, attr="fsync"):
                    pytest.fail(
                        "perf-002 violation: raw os.fsync call after "
                        f"for-loop at line {call.lineno}; must use _fsync_chain"
                    )

    def test_fsync_chain_call_uses_chain_path_arg(self, ctl_tree):
        """The single _fsync_chain call after the loop must pass
        chain_path (the same Path used by the locked file)."""
        fn = _find_function(ctl_tree, "cmd_run_task_receipt_chain")
        loop = self._find_pending_for_loop(fn)
        enclosing_try: ast.Try | None = None
        for node in ast.walk(fn):
            if isinstance(node, ast.Try):
                for descendant in ast.walk(node):
                    if descendant is loop:
                        enclosing_try = node
                        break
                if enclosing_try is not None:
                    break
        assert enclosing_try is not None
        loop_end = loop.end_lineno

        for stmt in enclosing_try.body:
            if stmt.lineno <= loop_end:
                continue
            for call in _walk_calls(stmt):
                if _is_call_to(call, name="_fsync_chain"):
                    assert len(call.args) == 1, (
                        "_fsync_chain should be called with exactly one positional arg"
                    )
                    arg = call.args[0]
                    assert isinstance(arg, ast.Name) and arg.id == "chain_path", (
                        f"_fsync_chain must be called with chain_path; got {ast.dump(arg)}"
                    )

    def test_fsync_chain_imported_from_lib_chain(self, ctl_tree):
        """The function must import _fsync_chain alongside _append_entry_unlocked."""
        fn = _find_function(ctl_tree, "cmd_run_task_receipt_chain")
        # Look for `from lib_chain import ... _fsync_chain ...`
        found = False
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module == "lib_chain":
                names = {alias.name for alias in node.names}
                if "_fsync_chain" in names:
                    found = True
                    # Sanity: must also import _append_entry_unlocked still
                    assert "_append_entry_unlocked" in names, (
                        "_append_entry_unlocked import was lost"
                    )
                    break
        assert found, "cmd_run_task_receipt_chain must import _fsync_chain from lib_chain"


# ---------------------------------------------------------------------------
# perf-003 oracle + correctness tests for _read_tail_prev_sha
# ---------------------------------------------------------------------------


def _ref_read_tail_prev_sha(path: Path) -> str:
    """Original full-read implementation, used as oracle for perf-003 tests."""
    from lib_chain import _GENESIS_PREV_SHA256, _canonical_json

    if not path.exists():
        return _GENESIS_PREV_SHA256
    text = path.read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return _GENESIS_PREV_SHA256
    try:
        rec = json.loads(lines[-1])
    except json.JSONDecodeError:
        return _GENESIS_PREV_SHA256
    return hashlib.sha256(_canonical_json(rec).encode("utf-8")).hexdigest()


def _make_task_dir(base: Path, task_id: str = "task-20260504-004") -> Path:
    task_dir = base / ".dynos" / task_id
    (task_dir / "receipts").mkdir(parents=True)
    return task_dir


@pytest.fixture
def project_secret(monkeypatch):
    """Force a deterministic project secret so HMAC keying is stable
    across the test process."""
    monkeypatch.setenv("DYNOS_EVENT_SECRET", "fixed-project-secret")
    return "fixed-project-secret"


def _build_chain(task_dir: Path, n: int) -> Path:
    """Build a real chain of length `n` via lib_chain._append_entry,
    creating a unique receipt file per entry. Returns chain path."""
    from lib_chain import _append_entry

    receipts_dir = task_dir / "receipts"
    receipts_dir.mkdir(parents=True, exist_ok=True)

    for i in range(n):
        rp = receipts_dir / f"step-{i:04d}.json"
        # Distinct content per receipt so sha256 is unique.
        rp.write_text(json.dumps({"step": f"step-{i:04d}", "i": i}) + "\n",
                      encoding="utf-8")
        _append_entry(task_dir, f"step-{i:04d}", "receipt", rp)

    chain_path = task_dir / "task-receipt-chain.jsonl"
    assert chain_path.exists()
    return chain_path


class TestPerf003ParametricCorrectness:
    """AC 10, AC 11, AC 12: `_read_tail_prev_sha` reverse-seek output
    matches the full-read oracle for chain lengths {1, 5, 100, 1000}.

    AC 12 (forensics invariant) is verified by the n=100 case via the
    oracle equality check — both implementations must produce the same
    prev_sha256 for an identical chain file built by `_append_entry`.
    """

    @pytest.mark.parametrize("n", [1, 5, 100, 1000])
    def test_reverse_seek_matches_full_read(self, tmp_path, project_secret, n):
        from lib_chain import _read_tail_prev_sha

        task_dir = _make_task_dir(tmp_path, task_id=f"task-len-{n}")
        chain_path = _build_chain(task_dir, n)

        # Confirm the chain has exactly n entries (sanity).
        lines = chain_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == n, f"expected {n} chain entries, got {len(lines)}"

        actual = _read_tail_prev_sha(chain_path)
        expected = _ref_read_tail_prev_sha(chain_path)

        assert actual == expected, (
            f"perf-003 mismatch at n={n}: "
            f"reverse-seek={actual!r}, full-read oracle={expected!r}"
        )

        # Hex-string structure check — must be 64-char sha256.
        assert isinstance(actual, str)
        assert len(actual) == 64
        int(actual, 16)  # must be valid hex; raises ValueError otherwise


class TestPerf003EdgeCases:
    """AC 10: documented edge cases — empty file, single line w/ and w/o
    trailing newline, malformed JSON last line, multiple trailing newlines.

    The reverse-seek implementation must return the same value as the
    full-read oracle for each. For pathological cases (empty / malformed),
    both implementations must return _GENESIS_PREV_SHA256.
    """

    def test_missing_file_returns_genesis(self, tmp_path):
        from lib_chain import _GENESIS_PREV_SHA256, _read_tail_prev_sha
        path = tmp_path / "does-not-exist.jsonl"
        assert not path.exists()
        actual = _read_tail_prev_sha(path)
        assert actual == _GENESIS_PREV_SHA256
        assert actual == _ref_read_tail_prev_sha(path)

    def test_empty_file_returns_genesis(self, tmp_path):
        from lib_chain import _GENESIS_PREV_SHA256, _read_tail_prev_sha
        path = tmp_path / "empty.jsonl"
        path.write_bytes(b"")
        assert path.stat().st_size == 0
        actual = _read_tail_prev_sha(path)
        assert actual == _GENESIS_PREV_SHA256
        assert actual == _ref_read_tail_prev_sha(path)

    def test_single_line_no_trailing_newline(self, tmp_path):
        """A single JSON line with no trailing newline — both implementations
        should compute the same prev_sha256 (and not return genesis)."""
        from lib_chain import (
            _GENESIS_PREV_SHA256,
            _canonical_json,
            _read_tail_prev_sha,
        )

        path = tmp_path / "single-no-nl.jsonl"
        rec = {"step": "alpha", "kind": "receipt", "i": 1}
        path.write_text(json.dumps(rec), encoding="utf-8")
        # Confirm precondition: no trailing newline byte
        assert not path.read_bytes().endswith(b"\n")

        expected = hashlib.sha256(_canonical_json(rec).encode("utf-8")).hexdigest()
        actual = _read_tail_prev_sha(path)
        assert actual == expected, (
            f"single-line-no-trailing-nl mismatch: got {actual!r}, expected {expected!r}"
        )
        # Oracle parity
        assert actual == _ref_read_tail_prev_sha(path)
        # And the answer must NOT be the genesis sentinel for a real entry.
        assert actual != _GENESIS_PREV_SHA256

    def test_single_line_with_trailing_newline(self, tmp_path):
        from lib_chain import (
            _GENESIS_PREV_SHA256,
            _canonical_json,
            _read_tail_prev_sha,
        )

        path = tmp_path / "single-with-nl.jsonl"
        rec = {"step": "beta", "kind": "receipt", "i": 2}
        path.write_text(json.dumps(rec) + "\n", encoding="utf-8")
        assert path.read_bytes().endswith(b"\n")

        expected = hashlib.sha256(_canonical_json(rec).encode("utf-8")).hexdigest()
        actual = _read_tail_prev_sha(path)
        assert actual == expected
        assert actual == _ref_read_tail_prev_sha(path)
        assert actual != _GENESIS_PREV_SHA256

    def test_malformed_json_last_line_returns_genesis(self, tmp_path):
        """If the final non-blank line is not valid JSON, the function
        falls back to genesis (preserves existing public contract)."""
        from lib_chain import _GENESIS_PREV_SHA256, _read_tail_prev_sha

        path = tmp_path / "malformed-last.jsonl"
        good = json.dumps({"step": "gamma", "i": 1})
        # Last line is corrupted (truncated brace)
        bad = '{"step": "delta", "i":'  # invalid JSON
        path.write_text(good + "\n" + bad + "\n", encoding="utf-8")

        actual = _read_tail_prev_sha(path)
        assert actual == _GENESIS_PREV_SHA256, (
            "malformed last line must yield genesis sentinel"
        )
        assert actual == _ref_read_tail_prev_sha(path)

    def test_multiple_trailing_newlines(self, tmp_path):
        """A real entry followed by multiple blank lines (multiple
        trailing newlines). The function must rstrip blanks and return
        the prev_sha256 of the final non-empty entry."""
        from lib_chain import (
            _GENESIS_PREV_SHA256,
            _canonical_json,
            _read_tail_prev_sha,
        )

        path = tmp_path / "multi-trailing-nl.jsonl"
        rec = {"step": "epsilon", "kind": "receipt", "i": 5}
        path.write_text(json.dumps(rec) + "\n\n\n\n", encoding="utf-8")
        assert path.read_bytes().endswith(b"\n\n\n\n")

        expected = hashlib.sha256(_canonical_json(rec).encode("utf-8")).hexdigest()
        actual = _read_tail_prev_sha(path)
        assert actual == expected, (
            f"multi-trailing-nl mismatch: got {actual!r}, expected {expected!r}"
        )
        assert actual == _ref_read_tail_prev_sha(path)
        assert actual != _GENESIS_PREV_SHA256

    def test_only_blank_lines_returns_genesis(self, tmp_path):
        """Defensive: file with only newlines (no real content) returns genesis."""
        from lib_chain import _GENESIS_PREV_SHA256, _read_tail_prev_sha

        path = tmp_path / "only-blanks.jsonl"
        path.write_text("\n\n\n", encoding="utf-8")

        actual = _read_tail_prev_sha(path)
        assert actual == _GENESIS_PREV_SHA256
        assert actual == _ref_read_tail_prev_sha(path)


# ---------------------------------------------------------------------------
# Forensics invariant: 100-entry chain, validate_chain returns "valid"
# ---------------------------------------------------------------------------


class TestPerf003ForensicsInvariant:
    """AC 12: A 100-entry chain built via the updated `_append_entry`
    must validate as `status="valid"` end-to-end. This proves that the
    perf changes (fsync placement + reverse-seek tail read) do not
    perturb the prev_sha256 linkage or HMAC _sig that validate_chain checks.
    """

    def test_validate_chain_status_valid_for_100_entry_chain(
        self, tmp_path, project_secret
    ):
        from lib_chain import validate_chain

        task_dir = _make_task_dir(tmp_path, task_id="task-forensics-100")
        chain_path = _build_chain(task_dir, 100)

        # Sanity: chain has 100 entries.
        lines = chain_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 100

        result = validate_chain(task_dir)
        assert result.status == "valid", (
            f"forensics invariant failed: status={result.status!r}, "
            f"index={result.first_failed_index}, "
            f"field={result.first_failed_field}, "
            f"reason={result.error_reason}"
        )
        assert result.first_failed_index is None
        assert result.first_failed_field is None
        assert result.error_reason is None

    def test_tail_sha_round_trip_matches_oracle(self, tmp_path, project_secret):
        """Belt-and-braces forensics check: the prev_sha256 of the
        100th entry (i.e., what the reverse-seek would compute as the
        prior-link for entry #101) is identical to what the full-read
        oracle computes from the same on-disk bytes."""
        from lib_chain import _read_tail_prev_sha

        task_dir = _make_task_dir(tmp_path, task_id="task-roundtrip-100")
        chain_path = _build_chain(task_dir, 100)

        actual = _read_tail_prev_sha(chain_path)
        expected = _ref_read_tail_prev_sha(chain_path)
        assert actual == expected
        # And the next append's prev_sha256 must equal this value.
        from lib_chain import _append_entry

        rp = task_dir / "receipts" / "step-extra.json"
        rp.write_text('{"step": "extra"}', encoding="utf-8")
        _append_entry(task_dir, "step-extra", "receipt", rp)

        new_lines = chain_path.read_text(encoding="utf-8").splitlines()
        assert len(new_lines) == 101
        last_entry = json.loads(new_lines[-1])
        assert last_entry["prev_sha256"] == actual, (
            "newly-appended entry's prev_sha256 must equal the tail sha "
            "computed before the append"
        )
