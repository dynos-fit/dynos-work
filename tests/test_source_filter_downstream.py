"""Downstream-consumer tests for the ``_source`` filter wiring on
``hooks/lib_core.py:collect_retrospectives``.

Behaviour under test (already implemented in lib_core, this suite is the
regression sentinel for it):

  * Default call (``include_unverified=False``) MUST exclude entries
    tagged ``_source == "persistent-unverified"`` (i.e. persistent
    retros without a matching ``retrospective_flushed`` event).
  * Passing ``include_unverified=True`` MUST return them.
  * Three downstream consumers — ``memory/policy_engine.py``,
    ``memory/agent_generator.py``, ``memory/postmortem_improve.py`` —
    MUST never opt into unverified entries by passing
    ``include_unverified=True``.  Verified via AST scan so the contract
    holds even if a future refactor moves the call site around inside
    the file.
  * Regression sentinel: NO ``.py`` file under ``hooks/`` or ``memory/``
    (other than ``hooks/lib_core.py``, the authoritative source) may
    bypass ``collect_retrospectives`` by direct-globbing the persistent
    retrospectives directory for ``*.json``.  Bypassing the function
    bypasses the SEC-003 hash-verification + AC21 unverified-tagging
    pipeline, which would silently re-introduce the very leak this task
    closes.
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"
MEMORY_DIR = REPO_ROOT / "memory"
sys.path.insert(0, str(HOOKS_DIR))
sys.path.insert(0, str(MEMORY_DIR))


# ---------------------------------------------------------------------------
# Setup helpers — mirror tests/test_collect_retrospectives_union.py pattern.
# ---------------------------------------------------------------------------


def _setup_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create root + .dynos + persistent retros dir.

    Redirects ``_persistent_project_dir`` to an isolated path inside
    ``tmp_path`` so persistent retro lookups never touch the user's real
    DYNOS_HOME.  Clears the module-level memo cache so each test starts
    cold and re-runs the ingest path.
    """
    import lib_core  # noqa: PLC0415

    root = tmp_path / "project"
    root.mkdir()
    (root / ".dynos").mkdir()
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    monkeypatch.setattr(lib_core, "_persistent_project_dir", lambda r: persistent)
    lib_core._COLLECT_RETRO_CACHE.clear()
    return root


def _write_persistent_retro(
    root: Path, task_id: str, *, with_flush_event: bool
) -> Path:
    """Write a persistent retro.  When ``with_flush_event=True`` also
    write a matching content-hashed ``retrospective_flushed`` event so
    SEC-003 verification flips ``_source`` from ``persistent-unverified``
    to ``persistent``.
    """
    import lib_core  # noqa: PLC0415

    persistent = lib_core._persistent_project_dir(root)
    retros_dir = persistent / "retrospectives"
    retros_dir.mkdir(parents=True, exist_ok=True)
    retro_path = retros_dir / f"{task_id}.json"
    retro_data = {
        "task_id": task_id,
        "task_type": "feature",
        "task_risk_level": "low",
        "findings_by_auditor": {},
        "repair_cycle_count": 0,
        "quality_score": 0.9,
    }
    retro_path.write_text(json.dumps(retro_data, indent=2))

    if with_flush_event:
        from lib_receipts import hash_file  # noqa: PLC0415

        sha = hash_file(retro_path)
        events_path = root / ".dynos" / "events.jsonl"
        evt = {
            "event": "retrospective_flushed",
            "task": task_id,
            "task_id": task_id,
            "sha256": sha,
        }
        with events_path.open("a") as f:
            f.write(json.dumps(evt) + "\n")
    return retro_path


def _collect_retrospectives_calls(src: str) -> list[ast.Call]:
    """Return every AST Call node whose function reference resolves to
    the bare-or-attribute name ``collect_retrospectives``.
    """
    tree = ast.parse(src)
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            continue
        if name == "collect_retrospectives":
            out.append(node)
    return out


def _assert_no_include_unverified_true(src: str, label: str) -> None:
    """Assert no collect_retrospectives(...) call in ``src`` passes
    ``include_unverified=True``.  A literal ``False`` is allowed (still
    the safe default).  Anything non-literal (variable, *, **) is
    rejected because we cannot prove safety statically.
    """
    calls = _collect_retrospectives_calls(src)
    assert calls, (
        f"{label}: expected at least one collect_retrospectives(...) "
        f"call (this guard becomes useless if the consumer stops calling "
        f"the function altogether — re-target the test)."
    )
    for call in calls:
        for kw in call.keywords:
            if kw.arg == "include_unverified":
                # Only literal False is acceptable — anything else
                # potentially flips the filter off.
                assert isinstance(kw.value, ast.Constant), (
                    f"{label}: collect_retrospectives "
                    f"include_unverified must be a literal "
                    f"False if present; got non-literal "
                    f"{ast.dump(kw.value)!r}"
                )
                assert kw.value.value is False, (
                    f"{label}: collect_retrospectives must not be "
                    f"called with include_unverified=True (got "
                    f"include_unverified={kw.value.value!r}); doing so "
                    f"re-admits the persistent-unverified entries that "
                    f"this filter is designed to suppress."
                )


# ---------------------------------------------------------------------------
# 1. TestDefaultFilter — runtime behavior of the wired filter.
# ---------------------------------------------------------------------------


class TestDefaultFilter:
    """Direct runtime exercise of collect_retrospectives default-vs-opt-in."""

    def test_default_call_excludes_persistent_unverified(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persistent retro with NO flush event must be filtered out
        of the default-call result, while a persistent retro WITH a
        matching flush event must remain.
        """
        root = _setup_project(tmp_path, monkeypatch)
        _write_persistent_retro(root, "task-verified", with_flush_event=True)
        _write_persistent_retro(root, "task-unverified", with_flush_event=False)

        import lib_core  # noqa: PLC0415

        lib_core._COLLECT_RETRO_CACHE.clear()
        retros = lib_core.collect_retrospectives(root)
        ids = {r.get("task_id") for r in retros}

        assert "task-verified" in ids, (
            f"verified retro must be present in default call; got {ids!r}"
        )
        assert "task-unverified" not in ids, (
            f"default call must exclude persistent-unverified entries; "
            f"got {ids!r}"
        )
        # Belt-and-braces: confirm no entry leaks the unverified tag.
        sources = {r.get("_source") for r in retros}
        assert "persistent-unverified" not in sources, (
            f"no _source=persistent-unverified entry may leak through "
            f"the default filter; saw sources={sources!r}"
        )

    def test_include_unverified_true_returns_both(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The opt-in escape hatch must surface unverified entries."""
        root = _setup_project(tmp_path, monkeypatch)
        _write_persistent_retro(root, "task-verified", with_flush_event=True)
        _write_persistent_retro(root, "task-unverified", with_flush_event=False)

        import lib_core  # noqa: PLC0415

        lib_core._COLLECT_RETRO_CACHE.clear()
        retros = lib_core.collect_retrospectives(root, include_unverified=True)
        ids = {r.get("task_id") for r in retros}

        assert ids == {"task-verified", "task-unverified"}, (
            f"include_unverified=True must surface both entries; got {ids!r}"
        )
        # The unverified entry must carry its diagnostic _source tag.
        unverified = [r for r in retros if r.get("task_id") == "task-unverified"]
        assert len(unverified) == 1
        assert unverified[0].get("_source") == "persistent-unverified", (
            f"opt-in path must preserve the persistent-unverified tag; "
            f"got _source={unverified[0].get('_source')!r}"
        )

    def test_cached_result_still_filters_on_subsequent_default_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Regression guard: the memo cache stores the unfiltered list.
        A second default-call (cache hit) must still strip the
        persistent-unverified entries — otherwise turning on the cache
        would silently undo the filter.
        """
        root = _setup_project(tmp_path, monkeypatch)
        _write_persistent_retro(root, "task-v", with_flush_event=True)
        _write_persistent_retro(root, "task-u", with_flush_event=False)

        import lib_core  # noqa: PLC0415

        # First call (cold path) populates the cache.
        first = lib_core.collect_retrospectives(root)
        # Second call (hot path / cache hit).
        second = lib_core.collect_retrospectives(root)

        first_ids = {r.get("task_id") for r in first}
        second_ids = {r.get("task_id") for r in second}
        assert first_ids == second_ids == {"task-v"}, (
            f"both cold and hot calls must filter unverified; "
            f"got cold={first_ids!r} hot={second_ids!r}"
        )

        # And a hot-path opt-in call must still surface the unverified
        # entry (same cache, different filter at return time).
        opt_in = lib_core.collect_retrospectives(root, include_unverified=True)
        assert {r.get("task_id") for r in opt_in} == {"task-v", "task-u"}, (
            "include_unverified=True on cache-hit path must still surface "
            "both rows from the cached unfiltered list"
        )


# ---------------------------------------------------------------------------
# 2. TestPolicyEngineFilter — AST guarantee on memory/policy_engine.py.
# ---------------------------------------------------------------------------


class TestPolicyEngineFilter:
    """Static guarantee that policy_engine never opts into unverified."""

    def test_policy_engine_never_calls_with_include_unverified_true(self) -> None:
        src = (MEMORY_DIR / "policy_engine.py").read_text()
        _assert_no_include_unverified_true(src, "memory/policy_engine.py")


# ---------------------------------------------------------------------------
# 3. TestAgentGeneratorFilter — AST guarantee on memory/agent_generator.py.
# ---------------------------------------------------------------------------


class TestAgentGeneratorFilter:
    """Static guarantee that agent_generator never opts into unverified."""

    def test_agent_generator_never_calls_with_include_unverified_true(self) -> None:
        src = (MEMORY_DIR / "agent_generator.py").read_text()
        _assert_no_include_unverified_true(src, "memory/agent_generator.py")


# ---------------------------------------------------------------------------
# 4. TestPostmortemImproveFilter — AST guarantee on
#    memory/postmortem_improve.py.
# ---------------------------------------------------------------------------


class TestPostmortemImproveFilter:
    """Static guarantee that postmortem_improve never opts into unverified."""

    def test_postmortem_improve_never_calls_with_include_unverified_true(
        self,
    ) -> None:
        src = (MEMORY_DIR / "postmortem_improve.py").read_text()
        _assert_no_include_unverified_true(src, "memory/postmortem_improve.py")


# ---------------------------------------------------------------------------
# 4b. TestPostmortemFilter — AST guarantee on memory/postmortem.py.
# ---------------------------------------------------------------------------


class TestPostmortemFilter:
    """Static guarantee that postmortem never opts into unverified.

    ``memory/postmortem.py`` is the rule-mining sibling of
    ``postmortem_improve.py`` and consumes ``collect_retrospectives``
    output to build prevention rules.  Same trust-boundary contract
    applies: ingesting a ``persistent-unverified`` retro into rule
    mining would let a tampered/unflushed retro shape future routing
    decisions, which is precisely the leak this suite closes.
    """

    def test_postmortem_never_calls_with_include_unverified_true(self) -> None:
        src = (MEMORY_DIR / "postmortem.py").read_text()
        _assert_no_include_unverified_true(src, "memory/postmortem.py")


# ---------------------------------------------------------------------------
# 5. TestRegressionSentinel — repo-wide static scan for direct-glob
#    bypasses of collect_retrospectives.
# ---------------------------------------------------------------------------


# Immutable allowlist (per [cq] rule: hygiene allowlists must be
# frozensets/tuples, not mutable lists). Each entry must carry an
# explicit, narrowly-scoped reason — adding a new entry without one is
# a code-review red flag because every additional bypass widens the
# attack surface this sentinel was written to close.
#
# Allowed bypasses:
#   * hooks/lib_core.py — the authoritative implementation of
#     collect_retrospectives itself; it MUST glob the persistent dir
#     directly to feed the function.
#   * hooks/check_deferred_findings.py — counts files (`len(list(...))`)
#     for a TTL baseline.  It never reads retro CONTENT and therefore
#     cannot leak ``persistent-unverified`` data into downstream policy
#     decisions.  See ``_current_retrospective_count`` (count-only,
#     does not parse JSON).  If this file ever starts loading retro
#     content, remove it from the allowlist and route it through
#     ``collect_retrospectives`` instead.
_SENTINEL_ALLOWLIST: frozenset[Path] = frozenset({
    HOOKS_DIR / "lib_core.py",
    HOOKS_DIR / "check_deferred_findings.py",
})


class TestRegressionSentinel:
    """Repository-wide structural sentinel.

    No file in ``hooks/`` or ``memory/`` (other than the authoritative
    ``hooks/lib_core.py``) may pair a ``retrospectives`` reference with a
    ``glob("*.json")`` (or single-quote variant) within ~200 chars.
    Doing so almost certainly indicates a direct read of the persistent
    retros dir that bypasses the SEC-003 hash check and the AC21
    unverified-tagging pipeline.
    """

    def test_no_direct_persistent_retro_glob_outside_lib_core(self) -> None:
        violations: list[str] = []
        scanned = 0
        for base in ("hooks", "memory"):
            base_dir = REPO_ROOT / base
            if not base_dir.exists():
                continue
            for py_file in base_dir.rglob("*.py"):
                if "__pycache__" in py_file.parts:
                    continue
                if py_file in _SENTINEL_ALLOWLIST:
                    continue
                scanned += 1
                src = py_file.read_text(errors="ignore")
                if "retrospectives" not in src:
                    continue
                if 'glob("*.json")' not in src and "glob('*.json')" not in src:
                    continue
                # Both tokens are present in the file — but they may be
                # unrelated.  Require proximity: any ``retrospectives``
                # occurrence within 200 chars of a ``glob`` + ``.json``
                # pair is treated as a bypass.
                for m in re.finditer(r"retrospectives", src):
                    snippet = src[max(0, m.start() - 200) : m.end() + 200]
                    if "glob" in snippet and ".json" in snippet:
                        violations.append(str(py_file.relative_to(REPO_ROOT)))
                        break

        assert scanned > 0, (
            "sentinel scanned zero files — repository layout changed; "
            "re-target the scan paths"
        )
        assert not violations, (
            "Direct persistent-retro reads bypass the "
            "collect_retrospectives filter (SEC-003 + AC21). Funnel all "
            "persistent-retros reads through "
            "hooks/lib_core.collect_retrospectives instead. "
            f"Violations: {violations!r}"
        )

    def test_allowlist_is_immutable(self) -> None:
        """Hygiene rule [cq]: allowlists must be immutable so a future
        edit cannot silently widen the bypass set.
        """
        assert isinstance(_SENTINEL_ALLOWLIST, frozenset), (
            f"allowlist must be a frozenset; got {type(_SENTINEL_ALLOWLIST).__name__}"
        )
        for entry in _SENTINEL_ALLOWLIST:
            assert entry.exists(), (
                f"allowlisted path must exist (stale entry indicates "
                f"refactor drift): {entry}"
            )

    def test_check_deferred_findings_remains_count_only(self) -> None:
        """The ``check_deferred_findings.py`` allowlist entry is
        justified ONLY because that module counts retrospective files
        without parsing their content.  If it ever begins reading retro
        CONTENT (json.load / json.loads / load_json on a retros path,
        or open()-then-read on a retros path), the bypass becomes
        dangerous — a persistent-unverified retro could leak into a
        decision path with no SEC-003 verification.  This guard fires
        loudly the moment that line is crossed.
        """
        path = HOOKS_DIR / "check_deferred_findings.py"
        src = path.read_text()
        # The file is allowed to mention retrospectives + glob (TTL
        # count). It is NOT allowed to parse those JSON files.
        forbidden_token_patterns = (
            r"json\.load\s*\(",
            r"json\.loads\s*\(",
            r"load_json\s*\(",
        )
        offending: list[str] = []
        for m in re.finditer(r"retrospectives", src):
            window = src[max(0, m.start() - 400) : m.end() + 400]
            for pattern in forbidden_token_patterns:
                if re.search(pattern, window):
                    offending.append(pattern)
        assert not offending, (
            f"check_deferred_findings.py is allowlisted only because it "
            f"counts retro files without parsing them.  Detected JSON-"
            f"parsing call(s) {offending!r} near a 'retrospectives' "
            f"reference — remove the allowlist entry and route the "
            f"caller through hooks/lib_core.collect_retrospectives "
            f"instead."
        )
