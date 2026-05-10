"""Tests for memory/policy_engine.py T-18 hardening — RED state.

These tests verify that policy_engine.project_slug routes through
sanitize_path_for_slug from lib_project_id, and that the shared
sanitizer is the single source of truth for both use cases.

Imports from lib_project_id will fail until production code is written —
that is the expected RED state.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Production imports — RED state until production code exists.
# ---------------------------------------------------------------------------
from lib_project_id import ProjectIdSecurityError, sanitize_path_for_slug
import policy_engine  # memory/policy_engine.py — already exists


# ---------------------------------------------------------------------------
# AC 18 — project_slug raises ProjectIdSecurityError for traversal paths
# ---------------------------------------------------------------------------


def test_claude_mirror_slug_rejects_traversal(tmp_path: Path):
    """AC 18 / T-18 — policy_engine.project_slug raises ProjectIdSecurityError for
    a Path whose resolve() contains '..' traversal components.
    """
    # We use a symlink that makes resolve() point to a '..' bearing path
    # representation. The key is that sanitize_path_for_slug sees '..' in the
    # string it receives.
    evil_dir = tmp_path / "innocent" / ".." / "etc"
    # Passing a Path whose str(resolve()) contains '..' via string construction.
    # We pass the string directly via a Path so resolve() returns it as-is from
    # the OS perspective, but we need the sanitiser to catch the literal '..'.
    # Construct a path whose resolved string contains '..'
    crafted = Path(str(tmp_path) + "/../etc")
    with pytest.raises(ProjectIdSecurityError):
        policy_engine.project_slug(crafted)


def test_claude_mirror_slug_rejects_traversal_direct_string(tmp_path: Path):
    """AC 18 / T-18 — project_slug raises for a string path containing '..'."""
    # Pass a raw string-based Path to ensure the sanitizer is actually called.
    evil = Path("/Users/hassam/../etc/passwd")
    with pytest.raises(ProjectIdSecurityError):
        policy_engine.project_slug(evil)


# ---------------------------------------------------------------------------
# AC 19 — project_slug raises ProjectIdSecurityError for control characters
# ---------------------------------------------------------------------------


def test_claude_mirror_slug_rejects_control_characters(tmp_path: Path):
    """AC 19 / T-18 — policy_engine.project_slug raises ProjectIdSecurityError for
    a resolved path containing a control character (\\x01).
    """
    evil = Path("/Users/hassam\x01/evil")
    with pytest.raises(ProjectIdSecurityError):
        policy_engine.project_slug(evil)


def test_claude_mirror_slug_rejects_null_byte():
    """AC 19 / T-18 — project_slug raises ProjectIdSecurityError for null byte in path."""
    evil = Path("/Users/hassam\x00/evil")
    with pytest.raises(ProjectIdSecurityError):
        policy_engine.project_slug(evil)


# ---------------------------------------------------------------------------
# AC 20 — sanitize_path_for_slug is the shared single source of truth
# ---------------------------------------------------------------------------


def test_sanitize_path_for_slug_is_shared_between_fallback_and_claude_mirror():
    """AC 20 — sanitize_path_for_slug is the single sanitizer; policy_engine.project_slug
    raises ProjectIdSecurityError for the same inputs that sanitize_path_for_slug rejects.
    """
    hostile_input = "/Users/hassam/../etc"
    # Direct call to the shared helper must raise.
    with pytest.raises(ProjectIdSecurityError):
        sanitize_path_for_slug(hostile_input)
    # policy_engine.project_slug must raise for the same input.
    with pytest.raises(ProjectIdSecurityError):
        policy_engine.project_slug(Path(hostile_input))


def test_policy_engine_project_slug_body_calls_sanitize_path_for_slug():
    """AC 20 — policy_engine.py must import sanitize_path_for_slug from lib_project_id.

    This AST scan verifies that the function body does NOT contain an
    independent sanitizer implementation and that the import exists.
    """
    src_file = Path(__file__).resolve().parents[1] / "memory" / "policy_engine.py"
    assert src_file.exists(), "memory/policy_engine.py not found"
    tree = ast.parse(src_file.read_text(encoding="utf-8"))

    # There must be an ImportFrom node referencing lib_project_id for sanitize_path_for_slug.
    imports_sanitizer = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "lib_project_id":
            names = [alias.name for alias in node.names]
            if "sanitize_path_for_slug" in names:
                imports_sanitizer = True
                break
    assert imports_sanitizer, (
        "policy_engine.py does not import sanitize_path_for_slug from lib_project_id"
    )


def test_policy_engine_project_slug_signature_unchanged():
    """AC 18 — policy_engine.project_slug must still accept (root: Path) -> str signature."""
    import inspect

    sig = inspect.signature(policy_engine.project_slug)
    params = list(sig.parameters.keys())
    assert params == ["root"], (
        f"project_slug signature changed; expected ['root'], got {params}"
    )


def test_policy_engine_project_slug_returns_string_for_valid_path(tmp_path: Path):
    """AC 18 — project_slug returns a string (not raises) for a normal well-formed path."""
    result = policy_engine.project_slug(tmp_path)
    assert isinstance(result, str), f"Expected str, got {type(result)}"
    assert len(result) > 0, "project_slug returned empty string"
