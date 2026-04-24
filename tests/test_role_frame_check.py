"""Tests for AC 8 and AC 9: _WRITE_ROLE constants and privileged-role frame check.

AC 8: hooks/lib_log.py, hooks/lib_receipts.py, hooks/ctl.py each define _WRITE_ROLE
      at module scope with values 'eventbus', 'receipt-writer', 'ctl' respectively.
      No inline role="eventbus" / role="receipt-writer" / role="ctl" literals at
      write sites.

AC 9: hooks/write_policy.py defines _PRIVILEGED_ROLE_MODULE_MAP: dict[str, frozenset[str]]
      with keys: eventbus, receipt-writer, ctl, scheduler, system.
      require_write_allowed walks the call stack when role is privileged; a call
      from an unauthorized module raises ValueError containing 'not authorized'.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path setup — make hooks/ importable
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]
HOOKS_DIR = ROOT / "hooks"

if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import lib_log
import lib_receipts
from write_policy import (
    WriteAttempt,
    _PRIVILEGED_ROLE_MODULE_MAP,
    require_write_allowed,
)

# Import ctl carefully — it has a __main__ guard so module-level code runs
# but main() does not.
import importlib.util as _importlib_util

_ctl_spec = _importlib_util.spec_from_file_location("ctl", HOOKS_DIR / "ctl.py")
_ctl_module = _importlib_util.module_from_spec(_ctl_spec)  # type: ignore[arg-type]
_ctl_spec.loader.exec_module(_ctl_module)  # type: ignore[union-attr]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_attempt(role: str) -> WriteAttempt:
    """Construct a minimal WriteAttempt targeting a non-task path."""
    return WriteAttempt(
        role=role,
        task_dir=None,
        path=Path("/tmp/dynos-test-dummy"),
        operation="modify",
        source="test",  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# AC 8 — _WRITE_ROLE constant value tests
# ---------------------------------------------------------------------------


class TestLibLogWriteRole:
    def test_lib_log_write_role_constant_value(self) -> None:
        """AC 8: lib_log._WRITE_ROLE must equal 'eventbus'."""
        assert lib_log._WRITE_ROLE == "eventbus"

    def test_lib_log_write_role_type_is_exactly_str(self) -> None:
        """AC 8: _WRITE_ROLE must be exactly str, not a subclass."""
        assert type(lib_log._WRITE_ROLE) is str


class TestLibReceiptsWriteRole:
    def test_lib_receipts_write_role_constant_value(self) -> None:
        """AC 8: lib_receipts._WRITE_ROLE must equal 'receipt-writer'."""
        assert lib_receipts._WRITE_ROLE == "receipt-writer"

    def test_lib_receipts_write_role_type_is_exactly_str(self) -> None:
        """AC 8: _WRITE_ROLE must be exactly str, not a subclass."""
        assert type(lib_receipts._WRITE_ROLE) is str


class TestCtlWriteRole:
    def test_ctl_write_role_constant_value(self) -> None:
        """AC 8: ctl._WRITE_ROLE must equal 'ctl'."""
        assert _ctl_module._WRITE_ROLE == "ctl"

    def test_ctl_write_role_type_is_exactly_str(self) -> None:
        """AC 8: _WRITE_ROLE must be exactly str, not a subclass."""
        assert type(_ctl_module._WRITE_ROLE) is str


# ---------------------------------------------------------------------------
# AC 8 — No inline role literals at write sites
# ---------------------------------------------------------------------------


class TestNoInlineRoleLiterals:
    """Grep the source files and assert no inline role literal strings appear
    outside of constant-definition lines.

    A constant-definition line matches: _WRITE_ROLE = "..."
    A non-constant occurrence would be role="eventbus" (at a call site).
    """

    @staticmethod
    def _non_constant_lines(filepath: Path, literal: str) -> list[tuple[int, str]]:
        """Return (lineno, text) pairs where `literal` appears but is NOT
        on a _WRITE_ROLE constant assignment line."""
        # Match exactly the quoted form: role="eventbus" etc.
        # A constant-def line looks like: _WRITE_ROLE = "eventbus"
        constant_def_pattern = re.compile(r"^\s*_WRITE_ROLE\s*=\s*")
        offending: list[tuple[int, str]] = []
        src = filepath.read_text(encoding="utf-8")
        for lineno, line in enumerate(src.splitlines(), start=1):
            if f'role="{literal}"' in line:
                if not constant_def_pattern.match(line):
                    offending.append((lineno, line.rstrip()))
        return offending

    def test_no_inline_role_eventbus_in_lib_log(self) -> None:
        """AC 8: lib_log.py must have no call-site role="eventbus" literals."""
        violations = self._non_constant_lines(HOOKS_DIR / "lib_log.py", "eventbus")
        assert violations == [], (
            f"Inline role=\"eventbus\" found in lib_log.py at lines: {violations}"
        )

    def test_no_inline_role_receipt_writer_in_lib_receipts(self) -> None:
        """AC 8: lib_receipts.py must have no call-site role="receipt-writer" literals."""
        violations = self._non_constant_lines(HOOKS_DIR / "lib_receipts.py", "receipt-writer")
        assert violations == [], (
            f"Inline role=\"receipt-writer\" found in lib_receipts.py at lines: {violations}"
        )

    def test_no_inline_role_ctl_in_ctl(self) -> None:
        """AC 8: ctl.py must have no call-site role="ctl" literals."""
        violations = self._non_constant_lines(HOOKS_DIR / "ctl.py", "ctl")
        assert violations == [], (
            f"Inline role=\"ctl\" found in ctl.py at lines: {violations}"
        )


# ---------------------------------------------------------------------------
# AC 9 — _PRIVILEGED_ROLE_MODULE_MAP structure tests
# ---------------------------------------------------------------------------


class TestPrivilegedRoleModuleMap:
    def test_map_is_a_dict_with_five_keys(self) -> None:
        """AC 9: _PRIVILEGED_ROLE_MODULE_MAP must be a dict with exactly 5 keys."""
        assert isinstance(_PRIVILEGED_ROLE_MODULE_MAP, dict)
        assert len(_PRIVILEGED_ROLE_MODULE_MAP) == 5, (
            f"Expected 5 keys, got {len(_PRIVILEGED_ROLE_MODULE_MAP)}"
        )

    def test_map_has_exactly_five_keys(self) -> None:
        """AC 9: map must contain exactly 5 keys."""
        assert len(_PRIVILEGED_ROLE_MODULE_MAP) == 5, (
            f"Expected 5 keys, got {len(_PRIVILEGED_ROLE_MODULE_MAP)}: "
            f"{list(_PRIVILEGED_ROLE_MODULE_MAP.keys())}"
        )

    def test_all_values_are_nonempty_frozensets(self) -> None:
        """AC 9: every value must be a non-empty frozenset of module-name strings."""
        for key, val in _PRIVILEGED_ROLE_MODULE_MAP.items():
            assert type(val) is frozenset, (
                f"Value for key {key!r} must be frozenset, got {type(val).__name__}"
            )
            assert len(val) > 0, f"frozenset for {key!r} must not be empty"
            for module_name in val:
                assert isinstance(module_name, str) and len(module_name) > 0, (
                    f"Module name in {key!r} allowlist must be non-empty str; got {module_name!r}"
                )

    def test_map_contains_eventbus(self) -> None:
        """AC 9: 'eventbus' key must exist and lib_log must be in its frozenset."""
        assert "eventbus" in _PRIVILEGED_ROLE_MODULE_MAP
        assert "lib_log" in _PRIVILEGED_ROLE_MODULE_MAP["eventbus"], (
            f"'lib_log' not found in eventbus allowlist: {_PRIVILEGED_ROLE_MODULE_MAP['eventbus']}"
        )

    def test_map_contains_receipt_writer(self) -> None:
        """AC 9: 'receipt-writer' key must exist and lib_receipts must be in its frozenset."""
        assert "receipt-writer" in _PRIVILEGED_ROLE_MODULE_MAP
        assert "lib_receipts" in _PRIVILEGED_ROLE_MODULE_MAP["receipt-writer"], (
            f"'lib_receipts' not found in receipt-writer allowlist: "
            f"{_PRIVILEGED_ROLE_MODULE_MAP['receipt-writer']}"
        )

    def test_map_contains_ctl(self) -> None:
        """AC 9: 'ctl' key must exist and 'ctl' must be in its frozenset."""
        assert "ctl" in _PRIVILEGED_ROLE_MODULE_MAP
        assert "ctl" in _PRIVILEGED_ROLE_MODULE_MAP["ctl"], (
            f"'ctl' not found in ctl allowlist: {_PRIVILEGED_ROLE_MODULE_MAP['ctl']}"
        )

    def test_map_contains_scheduler(self) -> None:
        """AC 9: 'scheduler' key must exist and 'scheduler' must be in its frozenset."""
        assert "scheduler" in _PRIVILEGED_ROLE_MODULE_MAP
        assert "scheduler" in _PRIVILEGED_ROLE_MODULE_MAP["scheduler"], (
            f"'scheduler' not found in scheduler allowlist: "
            f"{_PRIVILEGED_ROLE_MODULE_MAP['scheduler']}"
        )

    def test_map_contains_system(self) -> None:
        """AC 9: 'system' key must exist in the map."""
        assert "system" in _PRIVILEGED_ROLE_MODULE_MAP

    def test_map_system_value_is_nonempty(self) -> None:
        """AC 9: 'system' frozenset must be non-empty."""
        assert len(_PRIVILEGED_ROLE_MODULE_MAP["system"]) > 0, (
            "system allowlist must not be empty"
        )


# ---------------------------------------------------------------------------
# AC 9 — Frame check: require_write_allowed raises for unauthorized callers
# ---------------------------------------------------------------------------


class TestFrameCheck:
    """When a privileged role is used, require_write_allowed walks the call
    stack. Calls from this test module (not in any allowlist) must raise."""

    def test_privileged_role_from_test_module_raises_not_authorized(self) -> None:
        """AC 9: role='receipt-writer' from a non-allowlisted module raises ValueError."""
        attempt = _make_attempt("receipt-writer")
        with pytest.raises(ValueError):
            require_write_allowed(attempt, emit_event=False)

    def test_eventbus_role_from_test_module_raises(self) -> None:
        """AC 9: role='eventbus' from a non-allowlisted module raises ValueError."""
        attempt = _make_attempt("eventbus")
        with pytest.raises(ValueError):
            require_write_allowed(attempt, emit_event=False)

    def test_ctl_role_from_test_module_raises(self) -> None:
        """AC 9: role='ctl' from a non-allowlisted module raises ValueError."""
        attempt = _make_attempt("ctl")
        with pytest.raises(ValueError):
            require_write_allowed(attempt, emit_event=False)

    def test_non_privileged_role_from_test_module_does_not_raise(self) -> None:
        """AC 9: role='execute-inline' is not in the map, so no frame check fires.
        The write attempt targets /tmp (no task_dir), which is allowed for executors."""
        attempt = _make_attempt("execute-inline")
        result = require_write_allowed(attempt, emit_event=False)
        assert result is None  # require_write_allowed returns None on success

    def test_error_message_names_role(self) -> None:
        """AC 9: the raised ValueError must name the role in its message."""
        attempt = _make_attempt("receipt-writer")
        with pytest.raises(ValueError, match=r"receipt-writer"):
            require_write_allowed(attempt, emit_event=False)

    def test_error_message_contains_not_in_allowlist(self) -> None:
        """AC 9: the raised ValueError must contain 'not in allowlist'."""
        attempt = _make_attempt("receipt-writer")
        with pytest.raises(ValueError, match=r"not in allowlist"):
            require_write_allowed(attempt, emit_event=False)

    def test_scheduler_role_from_test_module_raises(self) -> None:
        """AC 9: role='scheduler' from a non-allowlisted module raises ValueError."""
        attempt = _make_attempt("scheduler")
        with pytest.raises(ValueError):
            require_write_allowed(attempt, emit_event=False)

    def test_system_role_from_test_module_raises(self) -> None:
        """AC 9: role='system' from a non-allowlisted module raises ValueError."""
        attempt = _make_attempt("system")
        with pytest.raises(ValueError):
            require_write_allowed(attempt, emit_event=False)

    def test_all_privileged_roles_raise_from_test_module(self) -> None:
        """AC 9 regression: every key in _PRIVILEGED_ROLE_MODULE_MAP raises
        when called from this test module — ensuring no key is accidentally
        missing from the frame-check logic."""
        for role in _PRIVILEGED_ROLE_MODULE_MAP:
            attempt = _make_attempt(role)
            with pytest.raises(ValueError, match=r"not in allowlist"):
                require_write_allowed(attempt, emit_event=False)
