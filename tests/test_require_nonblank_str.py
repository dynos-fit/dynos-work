"""Unit tests for require_nonblank_str in hooks/lib_validate.py.

Covers ACs 4, 5, 6, 7, 11 of task-20260504-006.

TDD contract: these tests FAIL until seg-1 adds require_nonblank_str to
hooks/lib_validate.py.  Do not implement the helper before these tests
are committed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "hooks"))

from lib_validate import require_nonblank_str  # noqa: E402


# ---------------------------------------------------------------------------
# AC 4: non-str input raises ValueError (not TypeError)
# ---------------------------------------------------------------------------

def test_non_str_raises_valueerror() -> None:
    """require_nonblank_str(123) raises ValueError, NOT TypeError (AC 4)."""
    with pytest.raises(ValueError, match="x must be a non-empty string"):
        require_nonblank_str(123, field_name="x")


# ---------------------------------------------------------------------------
# AC 5: empty string raises ValueError
# ---------------------------------------------------------------------------

def test_empty_str_raises_valueerror() -> None:
    """require_nonblank_str('') raises ValueError with expected message (AC 5)."""
    with pytest.raises(ValueError, match="x must be a non-empty string"):
        require_nonblank_str("", field_name="x")


# ---------------------------------------------------------------------------
# AC 6: whitespace-only string raises ValueError
# ---------------------------------------------------------------------------

def test_whitespace_only_raises_valueerror() -> None:
    """require_nonblank_str('   ') raises ValueError with expected message (AC 6)."""
    with pytest.raises(ValueError, match="x must be a non-empty string"):
        require_nonblank_str("   ", field_name="x")


# ---------------------------------------------------------------------------
# AC 7a: valid string with no surrounding whitespace returns same (stripped) value
# ---------------------------------------------------------------------------

def test_valid_str_returns_stripped() -> None:
    """require_nonblank_str('hello') returns 'hello' unchanged (AC 7)."""
    result = require_nonblank_str("hello", field_name="x")
    assert result == "hello"


# ---------------------------------------------------------------------------
# AC 7b: valid string with surrounding whitespace returns stripped value
# ---------------------------------------------------------------------------

def test_valid_str_with_padding_returns_stripped() -> None:
    """require_nonblank_str('  hello  ') returns 'hello' stripped (AC 7)."""
    result = require_nonblank_str("  hello  ", field_name="x")
    assert result == "hello"
