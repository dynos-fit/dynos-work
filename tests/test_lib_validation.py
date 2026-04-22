"""TDD-first tests for task-20260420-001 D1 — hooks/lib_validation.py.

Covers acceptance criteria 1-5 and 7 from the spec:

    AC1 require_nonblank  — rejects None, non-str, empty, whitespace-only
    AC2 require_sha256    — rejects None, non-str, len != 64, non [0-9a-f]
    AC3 require_stage_slug — enforces ^[A-Z][A-Z0-9_]*$
    AC4 require_task_id    — enforces ^task-[A-Za-z0-9][A-Za-z0-9_.-]*$
    AC5 require_enum       — rejects non-set values; message names field + sorted set
    AC7 this file has >=30 parametrize cases and exits 0 post-impl

All tests MUST fail on the unmodified tree because hooks/lib_validation.py
does not exist yet — collection itself will error, which IS the TDD-first
invariant: the failing-collection error is the red state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "hooks"))

# This import MUST fail today; that is the red state.
from lib_validation import (  # noqa: E402
    require_enum,
    require_nonblank,
    require_sha256,
    require_stage_slug,
    require_task_id,
)


# ---------------------------------------------------------------------------
# AC1 require_nonblank — rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        None,
        123,
        "",
        "   ",
        "\t\n",
        "\n",
        "\t",
        "  \t  ",
        [],
        {},
    ],
)
def test_require_nonblank_rejects(bad_value):
    """AC1: rejection cases with the exact error-message substring pinned."""
    with pytest.raises(ValueError) as exc_info:
        require_nonblank(bad_value, "myfield")
    msg = str(exc_info.value)
    assert "myfield" in msg
    assert "must be a non-empty string (whitespace-only rejected)" in msg


@pytest.mark.parametrize("good_value", ["x", "  x  ", "hello", "   hello world   "])
def test_require_nonblank_passes_through_identity(good_value):
    """AC1: returns the ORIGINAL (un-stripped) string — identity."""
    out = require_nonblank(good_value, "myfield")
    assert out is good_value


# ---------------------------------------------------------------------------
# AC2 require_sha256
# ---------------------------------------------------------------------------


_VALID_SHA = "a" * 64  # 64 lowercase hex
_VALID_SHA2 = "0123456789abcdef" * 4  # 64 lowercase hex, varied chars


@pytest.mark.parametrize(
    "bad_value",
    [
        None,
        "",
        123,
        "a" * 63,
        "a" * 65,
        "A" * 64,  # uppercase rejected
        ("a" * 63) + "G",  # non-hex char
        ("a" * 63) + "g",  # non-hex char
        ("a" * 63) + " ",
        ("a" * 63) + "-",
    ],
)
def test_require_sha256_rejects(bad_value):
    """AC2: rejection cases — None, non-str, wrong length, non-[0-9a-f]."""
    with pytest.raises(ValueError) as exc_info:
        require_sha256(bad_value, "my_sha")
    assert "my_sha" in str(exc_info.value)


@pytest.mark.parametrize("good_value", [_VALID_SHA, _VALID_SHA2, "0" * 64, "f" * 64])
def test_require_sha256_passes_returns_value(good_value):
    """AC2: valid 64-char lowercase hex passes, returns the value."""
    assert require_sha256(good_value, "my_sha") == good_value


# ---------------------------------------------------------------------------
# AC3 require_stage_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "",
        "spec_review",      # lowercase
        "SPEC-REVIEW",      # hyphen not in char class
        "../X",             # path traversal
        ".X",               # leading dot
        "X/Y",              # path separator
        "1STAGE",           # cannot start with digit
        " STAGE",           # leading space
        "STAGE ",           # trailing space
        None,
    ],
)
def test_require_stage_slug_rejects(bad_value):
    """AC3: enforces ^[A-Z][A-Z0-9_]*$ anchored, no path separators."""
    with pytest.raises(ValueError) as exc_info:
        require_stage_slug(bad_value, "stage")
    assert "stage" in str(exc_info.value)


@pytest.mark.parametrize("good_value", ["SPEC_REVIEW", "TDD", "X", "A0", "S_1_2_3"])
def test_require_stage_slug_passes(good_value):
    """AC3: valid slugs pass and the value is returned."""
    assert require_stage_slug(good_value, "stage") == good_value


# ---------------------------------------------------------------------------
# AC4 require_task_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "",
        "task-",            # nothing after the dash
        "task--x",          # leading dash character-class fails (char class disallows `-` leading)
        "task-/x",          # path separator
        "other-001",        # wrong prefix
        "task/x",           # missing dash
        "  task-001",       # leading whitespace
        "task-001  ",       # trailing whitespace
        None,
        123,
    ],
)
def test_require_task_id_rejects(bad_value):
    """AC4: enforces ^task-[A-Za-z0-9][A-Za-z0-9_.-]*$."""
    with pytest.raises(ValueError) as exc_info:
        require_task_id(bad_value, "task_id")
    assert "task_id" in str(exc_info.value)


@pytest.mark.parametrize(
    "good_value",
    [
        "task-20260420-001",
        "task-a",
        "task-a.b_c-d",
        "task-A1",
        "task-0",
    ],
)
def test_require_task_id_passes(good_value):
    """AC4: valid task IDs pass and return the value unchanged."""
    assert require_task_id(good_value, "task_id") == good_value


# ---------------------------------------------------------------------------
# AC5 require_enum
# ---------------------------------------------------------------------------


def test_require_enum_rejects_none():
    with pytest.raises(ValueError) as exc_info:
        require_enum(None, "color", {"a", "b"})
    msg = str(exc_info.value)
    assert "color" in msg
    # Sorted allowed_set repr must appear in the message.
    assert repr(sorted({"a", "b"})) in msg or "'a', 'b'" in msg


def test_require_enum_rejects_not_in_set():
    with pytest.raises(ValueError) as exc_info:
        require_enum("other", "color", {"a", "b"})
    msg = str(exc_info.value)
    assert "color" in msg
    assert repr(sorted({"a", "b"})) in msg or "'a', 'b'" in msg


def test_require_enum_rejects_nonstring():
    with pytest.raises(ValueError) as exc_info:
        require_enum(123, "color", {"a", "b"})
    assert "color" in str(exc_info.value)


@pytest.mark.parametrize("good_value", ["a", "b"])
def test_require_enum_passes(good_value):
    assert require_enum(good_value, "color", {"a", "b"}) == good_value


def test_require_enum_error_message_lists_sorted_allowed_set():
    """AC5: sorted allowed_set repr appears in the error message so the
    operator knows what was allowed. Non-sorted order in the message is a
    regression."""
    with pytest.raises(ValueError) as exc_info:
        require_enum("zz", "route", {"charlie", "alpha", "bravo"})
    msg = str(exc_info.value)
    # Either the full sorted list repr OR the comma-separated sorted names
    assert "alpha" in msg
    assert "bravo" in msg
    assert "charlie" in msg
    alpha_idx = msg.index("alpha")
    bravo_idx = msg.index("bravo")
    charlie_idx = msg.index("charlie")
    assert alpha_idx < bravo_idx < charlie_idx


def test_require_enum_accepts_frozenset():
    """AC5: both set and frozenset allowed (per signature)."""
    assert require_enum("a", "fld", frozenset({"a", "b"})) == "a"
