"""Regression tests for doc-staleness fixes — task-20260616-003.

These tests lock the fixed state of five documentation files against re-staling.
Each test asserts the FIXED state; a regression (restoring stale content) causes
the test to fail. Tests read files directly via Path; no production code is imported.

Coverage: ACs 1–6 from the spec.
"""
from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root resolution — same pattern as test_triage_demoted_rules.py
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _grep(text: str, pattern: str) -> list[str]:
    """Return all lines matching pattern (substring match)."""
    return [line for line in text.splitlines() if pattern in line]


# ===========================================================================
# AC 1 — docs/write-boundary-spec.md  (#13)
# ===========================================================================

class TestAC1WriteBoundarySpec:
    """AC1: Enforcement subsection names all three helpers; identifiers exist in source."""

    DOC = "docs/write-boundary-spec.md"
    SOURCE = "hooks/write_policy.py"

    def test_doc_contains_owning_task_dir(self):
        """doc references _owning_task_dir at least once."""
        text = _read(self.DOC)
        assert "_owning_task_dir" in text, (
            f"{self.DOC} must reference _owning_task_dir (Enforcement subsection)"
        )

    def test_doc_contains_is_cross_task_control_plane(self):
        """doc references _is_cross_task_control_plane at least once."""
        text = _read(self.DOC)
        assert "_is_cross_task_control_plane" in text, (
            f"{self.DOC} must reference _is_cross_task_control_plane (Enforcement subsection)"
        )

    def test_doc_contains_FRAMEWORK_ROLES(self):
        """doc references _FRAMEWORK_ROLES at least once."""
        text = _read(self.DOC)
        assert "_FRAMEWORK_ROLES" in text, (
            f"{self.DOC} must reference _FRAMEWORK_ROLES (Enforcement subsection)"
        )

    def test_doc_has_enforcement_subsection(self):
        """doc has an Enforcement subsection header (not just buried in prose)."""
        text = _read(self.DOC)
        # Accept any heading level: ##, ###, #### etc.
        headers = [line for line in text.splitlines()
                   if re.match(r"^#{1,6}\s+Enforcement", line, re.IGNORECASE)]
        assert headers, (
            f"{self.DOC} must contain an 'Enforcement' section header"
        )

    def test_source_has_owning_task_dir(self):
        """doc-matches-code: _owning_task_dir identifier exists in hooks/write_policy.py."""
        text = _read(self.SOURCE)
        assert "_owning_task_dir" in text, (
            f"{self.SOURCE} must define _owning_task_dir (doc-code sync)"
        )

    def test_source_has_is_cross_task_control_plane(self):
        """doc-matches-code: _is_cross_task_control_plane exists in hooks/write_policy.py."""
        text = _read(self.SOURCE)
        assert "_is_cross_task_control_plane" in text, (
            f"{self.SOURCE} must define _is_cross_task_control_plane (doc-code sync)"
        )

    def test_source_has_FRAMEWORK_ROLES(self):
        """doc-matches-code: _FRAMEWORK_ROLES exists in hooks/write_policy.py."""
        text = _read(self.SOURCE)
        assert "_FRAMEWORK_ROLES" in text, (
            f"{self.SOURCE} must define _FRAMEWORK_ROLES (doc-code sync)"
        )


# ===========================================================================
# AC 2 — docs/circuit-breaker.md  (#35)
# ===========================================================================

class TestAC2CircuitBreakerDoc:
    """AC2: doc uses constant names, stale literal thresholds are gone."""

    DOC = "docs/circuit-breaker.md"
    SOURCE = "hooks/circuit_breaker.py"

    REQUIRED_CONSTANTS = [
        "WASTED_SPAWN_ABORT_THRESHOLD",
        "SMALL_TASK_TOKEN_LIMIT",
        "BUGFIX_TOKEN_LIMIT",
        "SMALL_TASK_TOKEN_DOWNGRADE_THRESHOLD",
        "BUGFIX_TOKEN_DOWNGRADE_THRESHOLD",
    ]

    STALE_PHRASINGS = [
        ">= 3",
        ">= 1,000,000",
        ">= 5,000,000",
    ]

    def test_doc_contains_all_constant_names(self):
        """All five constant names appear in the circuit-breaker doc."""
        text = _read(self.DOC)
        missing = [c for c in self.REQUIRED_CONSTANTS if c not in text]
        assert not missing, (
            f"{self.DOC} is missing constant name(s): {missing}"
        )

    def test_doc_no_stale_wasted_spawn_threshold(self):
        """wasted_spawns_abort entry uses WASTED_SPAWN_ABORT_THRESHOLD, not a bare '>= 3' literal."""
        text = _read(self.DOC)
        # The stale form had the wasted_spawns_abort bullet written as ">= 3" inline.
        # The fix replaced that with the constant name. Verify the wasted_spawns_abort
        # line now contains the constant name, not a bare "3" without the constant.
        wasted_lines = [
            line for line in text.splitlines()
            if "wasted_spawns_abort" in line
        ]
        assert wasted_lines, (
            f"{self.DOC} must contain a 'wasted_spawns_abort' entry"
        )
        for line in wasted_lines:
            assert "WASTED_SPAWN_ABORT_THRESHOLD" in line, (
                f"wasted_spawns_abort line must reference WASTED_SPAWN_ABORT_THRESHOLD "
                f"(not a bare literal): {line!r}"
            )

    def test_doc_no_stale_one_million_threshold(self):
        """>= 1,000,000 stale token ceiling is gone from the doc threshold section."""
        text = _read(self.DOC)
        matches = _grep(text, ">= 1,000,000")
        assert not matches, (
            f"{self.DOC} still contains stale '>= 1,000,000' phrasing: {matches}"
        )

    def test_doc_no_stale_five_million_threshold(self):
        """>= 5,000,000 stale token ceiling is gone from the doc threshold section."""
        text = _read(self.DOC)
        matches = _grep(text, ">= 5,000,000")
        assert not matches, (
            f"{self.DOC} still contains stale '>= 5,000,000' phrasing: {matches}"
        )

    def test_source_has_all_constant_names(self):
        """doc-matches-code: all five constant names exist in hooks/circuit_breaker.py."""
        text = _read(self.SOURCE)
        missing = [c for c in self.REQUIRED_CONSTANTS if c not in text]
        assert not missing, (
            f"{self.SOURCE} is missing constant name(s): {missing} (doc-code sync)"
        )

    def test_doc_wasted_spawn_threshold_uses_constant_not_literal(self):
        """The wasted-spawns threshold is expressed via WASTED_SPAWN_ABORT_THRESHOLD, not a bare literal."""
        text = _read(self.DOC)
        lines_with_constant = _grep(text, "WASTED_SPAWN_ABORT_THRESHOLD")
        assert lines_with_constant, (
            f"{self.DOC} must reference WASTED_SPAWN_ABORT_THRESHOLD"
        )


# ===========================================================================
# AC 3 — docs/external-solution-gate.md  (#71)
# ===========================================================================

class TestAC3ExternalSolutionGate:
    """AC3: web-tool-log writes attributed to PostToolUse, not pre_tool_use."""

    DOC = "docs/external-solution-gate.md"

    def test_doc_does_not_mention_pre_tool_use_for_web_log(self):
        """pre_tool_use.py is not cited as the web-tool-log writer."""
        text = _read(self.DOC)
        # The stale claim was specifically about pre_tool_use.py writing the log;
        # check that pre_tool_use does not appear at all in this doc.
        matches = _grep(text, "pre_tool_use")
        assert not matches, (
            f"{self.DOC} still references 'pre_tool_use' (stale attribution): {matches}"
        )

    def test_doc_does_not_contain_at_call_time_for_web_log(self):
        """'at call time' phrasing in web-log context is gone."""
        text = _read(self.DOC)
        matches = _grep(text, "at call time")
        assert not matches, (
            f"{self.DOC} still contains 'at call time' phrasing: {matches}"
        )

    def test_doc_mentions_web_tool_log_py(self):
        """doc now attributes web-tool-log writes to hooks/web_tool_log.py."""
        text = _read(self.DOC)
        assert "web_tool_log.py" in text, (
            f"{self.DOC} must reference 'web_tool_log.py' as the PostToolUse hook"
        )

    def test_doc_mentions_posttooluse(self):
        """doc mentions PostToolUse as the hook type that writes the log."""
        text = _read(self.DOC)
        assert "PostToolUse" in text, (
            f"{self.DOC} must mention 'PostToolUse' hook"
        )


# ===========================================================================
# AC 4 — docs/foundry-hardening-spec.md  (#72)
# ===========================================================================

class TestAC4FoundryHardeningSpec:
    """AC4: sha256(path:hostname) derivation gone; secrets.token_hex(32) mentioned."""

    DOC = "docs/foundry-hardening-spec.md"
    SOURCE = "hooks/lib_log.py"

    def test_doc_no_stale_sha256_f_string_derivation(self):
        """The stale sha256(f\"{root.resolve()}:{platform.node()}\") string is gone."""
        text = _read(self.DOC)
        # Check for the sha256(f" pattern that was the stale derivation
        matches = _grep(text, 'sha256(f"')
        assert not matches, (
            f"{self.DOC} still contains stale sha256(f\"...\") derivation: {matches}"
        )

    def test_doc_no_stale_platform_node_derivation(self):
        """platform.node() in the context of the old derivation is gone from the doc."""
        text = _read(self.DOC)
        # Check for the sha256 + hostname/platform.node combination
        stale_lines = [
            line for line in text.splitlines()
            if "platform.node" in line and "sha256" in line
        ]
        assert not stale_lines, (
            f"{self.DOC} still references sha256+platform.node() derivation: {stale_lines}"
        )

    def test_doc_mentions_secrets_token_hex(self):
        """doc states that _resolve_event_secret now uses secrets.token_hex(32)."""
        text = _read(self.DOC)
        assert "secrets.token_hex" in text, (
            f"{self.DOC} must reference 'secrets.token_hex' as the current implementation"
        )

    def test_source_uses_secrets_token_hex(self):
        """doc-matches-code: hooks/lib_log.py actually uses secrets.token_hex(32)."""
        text = _read(self.SOURCE)
        assert "secrets.token_hex" in text, (
            f"{self.SOURCE} must use secrets.token_hex (doc-code sync)"
        )

    def test_doc_c2_resolved_note_present(self):
        """doc contains a note that C2 is resolved (not just aspirational)."""
        text = _read(self.DOC)
        # The fix should say C2 is resolved
        resolved_lines = [
            line for line in text.splitlines()
            if "C2" in line and any(kw in line.lower() for kw in ("resolved", "resolve", "closed", "fixed"))
        ]
        assert resolved_lines, (
            f"{self.DOC} must contain a C2-resolved note"
        )


# ===========================================================================
# AC 5 — docs/project-identity-design.md  (#73)
# ===========================================================================

class TestAC5ProjectIdentityDesign:
    """AC5: status banner reads IMPLEMENTED; lib_core.py:279-299 not cited as current seam."""

    DOC = "docs/project-identity-design.md"
    SOURCE = "hooks/lib_project_id.py"

    def test_doc_status_not_draft(self):
        """'Status: draft' no longer appears in the doc."""
        text = _read(self.DOC)
        matches = _grep(text, "Status: draft")
        assert not matches, (
            f"{self.DOC} still contains 'Status: draft': {matches}"
        )

    def test_doc_status_is_implemented(self):
        """doc header reads 'Status: IMPLEMENTED' (or equivalent case)."""
        text = _read(self.DOC)
        # Accept any casing of IMPLEMENTED
        implemented_lines = [
            line for line in text.splitlines()
            if re.search(r"status.*implemented", line, re.IGNORECASE)
        ]
        assert implemented_lines, (
            f"{self.DOC} must contain 'Status: IMPLEMENTED' or equivalent"
        )

    def test_doc_does_not_cite_lib_core_279_as_current_seam(self):
        """lib_core.py:279-299 is not cited as the current (unqualified) slug-derivation seam."""
        text = _read(self.DOC)
        # The stale citation was lib_core.py:279-299
        stale = _grep(text, "lib_core.py:279")
        assert not stale, (
            f"{self.DOC} still cites 'lib_core.py:279' as current seam: {stale}"
        )

    def test_doc_mentions_resolve_project_id(self):
        """doc references resolve_project_id as the current implementation seam."""
        text = _read(self.DOC)
        assert "resolve_project_id" in text, (
            f"{self.DOC} must reference 'resolve_project_id'"
        )

    def test_doc_mentions_lib_project_id_py(self):
        """doc references lib_project_id.py as the current module."""
        text = _read(self.DOC)
        assert "lib_project_id" in text, (
            f"{self.DOC} must reference 'lib_project_id' module"
        )

    def test_source_has_resolve_project_id(self):
        """doc-matches-code: resolve_project_id is defined in hooks/lib_project_id.py."""
        text = _read(self.SOURCE)
        assert "resolve_project_id" in text, (
            f"{self.SOURCE} must define resolve_project_id (doc-code sync)"
        )


# ===========================================================================
# AC 6 — README.md  (#34)
# ===========================================================================

class TestAC6ReadmeRegressionGuard:
    """AC6: README.md does not contain 'dynos global'; bin/dynos has no global) arm."""

    README = "README.md"
    DYNOS_BIN = "bin/dynos"

    def test_readme_no_dynos_global(self):
        """README.md must not contain 'dynos global' (regression guard for finding #34)."""
        text = _read(self.README)
        matches = _grep(text, "dynos global")
        assert not matches, (
            f"{self.README} must not contain 'dynos global' — regression of finding #34: {matches}"
        )

    def test_bin_dynos_no_global_arm(self):
        """bin/dynos must not contain a 'global)' case arm."""
        text = _read(self.DYNOS_BIN)
        matches = _grep(text, "global)")
        assert not matches, (
            f"{self.DYNOS_BIN} must not have a 'global)' arm: {matches}"
        )


# ===========================================================================
# AC7 — docs/write-boundary-spec.md + hooks/write_policy.py  (#task-20260616-004)
# ===========================================================================

class TestAC7WriteBoundaryDaemonRole:
    """AC7 (task-20260616-004): 'daemon' is enumerated in both doc sites and is a
    member of _FRAMEWORK_ROLES in hooks/write_policy.py.

    Two doc enumeration sites must list 'daemon':
      1. The _FRAMEWORK_ROLES bullet in the Enforcement subsection (line ~181).
      2. The "Initial role set" list in the Role-Based Policy section (line ~208).

    And the code must back this up: 'daemon' is in _FRAMEWORK_ROLES.
    """

    DOC = "docs/write-boundary-spec.md"
    SOURCE = "hooks/write_policy.py"

    def test_doc_enforcement_subsection_lists_daemon(self):
        """The _FRAMEWORK_ROLES bullet in the Enforcement subsection names 'daemon'.

        The Enforcement subsection describes _FRAMEWORK_ROLES as a frozenset
        whose members are listed inline in backtick code spans. The fixed state
        must include 'daemon' in that enumeration.
        """
        text = _read(self.DOC)
        # Locate the _FRAMEWORK_ROLES bullet — it contains the inline role list
        framework_roles_lines = [
            line for line in text.splitlines() if "_FRAMEWORK_ROLES" in line
        ]
        assert framework_roles_lines, (
            f"{self.DOC} must contain at least one line referencing _FRAMEWORK_ROLES"
        )
        # At least one of those lines must mention 'daemon'
        daemon_in_framework_roles = any("daemon" in line for line in framework_roles_lines)
        assert daemon_in_framework_roles, (
            f"{self.DOC}: the _FRAMEWORK_ROLES description must enumerate 'daemon'. "
            f"Lines found: {framework_roles_lines}"
        )

    def test_doc_role_based_policy_section_lists_daemon(self):
        """The Initial role set list in the Role-Based Policy section includes 'daemon'.

        The role-based policy section enumerates every recognized role as a
        markdown list item. 'daemon' must appear as one of those items after the
        fix. This is the second enumeration site.
        """
        text = _read(self.DOC)
        lines = text.splitlines()

        # Find the Role-Based Policy section header
        role_section_idx = None
        for i, line in enumerate(lines):
            if re.search(r"^#{1,6}\s+Role-Based Policy", line, re.IGNORECASE):
                role_section_idx = i
                break

        assert role_section_idx is not None, (
            f"{self.DOC} must contain a 'Role-Based Policy' section header"
        )

        # Collect lines from that section until the next same-level or higher header
        section_header_level = len(lines[role_section_idx]) - len(lines[role_section_idx].lstrip("#"))
        section_lines = []
        for line in lines[role_section_idx + 1:]:
            m = re.match(r"^(#{1,6})\s+", line)
            if m and len(m.group(1)) <= section_header_level:
                break
            section_lines.append(line)

        section_text = "\n".join(section_lines)
        assert "daemon" in section_text, (
            f"{self.DOC}: the Role-Based Policy 'Initial role set' list must include 'daemon'. "
            f"Section text (first 400 chars): {section_text[:400]!r}"
        )

    def test_write_policy_FRAMEWORK_ROLES_contains_daemon(self):
        """doc-matches-code: 'daemon' is a member of _FRAMEWORK_ROLES in hooks/write_policy.py.

        We parse the _FRAMEWORK_ROLES frozenset literal from the source text to
        confirm membership. Source parsing avoids Python-version-specific importlib
        issues with `from __future__ import annotations` and is sufficient because
        _FRAMEWORK_ROLES is a simple frozenset literal at module scope.
        """
        source = _read(self.SOURCE)

        # Locate the _FRAMEWORK_ROLES = frozenset({...}) block
        match = re.search(
            r"_FRAMEWORK_ROLES\s*=\s*frozenset\s*\(\s*\{([^}]+)\}\s*\)",
            source,
        )
        assert match is not None, (
            f"{self.SOURCE} must define _FRAMEWORK_ROLES as a frozenset literal"
        )

        # Extract the contents of the frozenset and collect string literals
        frozenset_body = match.group(1)
        members = re.findall(r'"([^"]+)"|\'([^\']+)\'', frozenset_body)
        role_names = {m[0] or m[1] for m in members}

        assert "daemon" in role_names, (
            f"{self.SOURCE}: 'daemon' must be a member of _FRAMEWORK_ROLES. "
            f"Parsed members from frozenset literal: {sorted(role_names)}"
        )

    def test_write_policy_FRAMEWORK_ROLES_is_frozenset_with_expected_core_roles(self):
        """_FRAMEWORK_ROLES literal contains all six expected core roles.

        Regression guard: adding 'daemon' must not remove the original core roles
        (ctl, scheduler, receipt-writer, eventbus, system). All six must be present.
        Uses source-text parsing to avoid importlib/Python-version issues.
        """
        source = _read(self.SOURCE)

        match = re.search(
            r"_FRAMEWORK_ROLES\s*=\s*frozenset\s*\(\s*\{([^}]+)\}\s*\)",
            source,
        )
        assert match is not None, (
            f"{self.SOURCE} must define _FRAMEWORK_ROLES as a frozenset literal"
        )

        frozenset_body = match.group(1)
        members = re.findall(r'"([^"]+)"|\'([^\']+)\'', frozenset_body)
        role_names = {m[0] or m[1] for m in members}

        required = {"ctl", "scheduler", "receipt-writer", "eventbus", "system", "daemon"}
        missing = required - role_names
        assert not missing, (
            f"_FRAMEWORK_ROLES is missing required roles: {missing}. "
            f"Parsed members from frozenset literal: {sorted(role_names)}"
        )
