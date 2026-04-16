#!/usr/bin/env python3
"""Deterministic compliance checks for the security-auditor.

Runs as a hook — invoked by the security-auditor agent via Bash.

Usage:
    python3 hooks/compliance_check.py --root <project-root> --task-dir <.dynos/task-id>

Checks:
  (a) License scan — flag GPL/AGPL deps in proprietary projects
  (b) SBOM generation — invoke cyclonedx-bom or syft if available
  (c) Dependency provenance — verify Sigstore attestations where available
  (d) Privacy-relevant code — detect missing data export / account deletion endpoints

Outputs JSON to stdout. The security-auditor merges these into its report.

Works for ANY project type — detects ecosystem from lock/manifest files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ecosystem detection
# ---------------------------------------------------------------------------

# Map of manifest/lock files to ecosystem name.  Checked in order — first hit
# wins, but we collect ALL ecosystems present.
ECOSYSTEM_MARKERS: dict[str, str] = {
    "package-lock.json": "npm",
    "yarn.lock": "npm",
    "pnpm-lock.yaml": "npm",
    "package.json": "npm",
    "Pipfile.lock": "python",
    "poetry.lock": "python",
    "requirements.txt": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "pyproject.toml": "python",
    "Gemfile.lock": "ruby",
    "Gemfile": "ruby",
    "go.sum": "go",
    "go.mod": "go",
    "Cargo.lock": "rust",
    "Cargo.toml": "rust",
    "composer.lock": "php",
    "composer.json": "php",
    "build.gradle": "java",
    "build.gradle.kts": "java",
    "pom.xml": "java",
    "pubspec.lock": "dart",
    "pubspec.yaml": "dart",
    "Package.resolved": "swift",
    "Package.swift": "swift",
    "mix.lock": "elixir",
}

# Licenses that are copyleft — problematic in proprietary projects.
COPYLEFT_LICENSES = {
    "GPL-2.0",
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "GPL-3.0",
    "GPL-3.0-only",
    "GPL-3.0-or-later",
    "AGPL-1.0",
    "AGPL-3.0",
    "AGPL-3.0-only",
    "AGPL-3.0-or-later",
    "LGPL-2.0",
    "LGPL-2.0-only",
    "LGPL-2.0-or-later",
    "LGPL-2.1",
    "LGPL-2.1-only",
    "LGPL-2.1-or-later",
    "LGPL-3.0",
    "LGPL-3.0-only",
    "LGPL-3.0-or-later",
    "SSPL-1.0",
    "EUPL-1.1",
    "EUPL-1.2",
    "OSL-3.0",
    "CPAL-1.0",
    "CPL-1.0",
    "CC-BY-SA-4.0",
}


def detect_ecosystems(root: Path) -> list[str]:
    """Return deduplicated list of ecosystems present in the project."""
    seen: set[str] = set()
    for marker, eco in ECOSYSTEM_MARKERS.items():
        if (root / marker).exists():
            seen.add(eco)
    return sorted(seen)


# ---------------------------------------------------------------------------
# (a) License check
# ---------------------------------------------------------------------------

def _run_cmd(cmd: list[str], cwd: str | None = None, timeout: int = 120) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
        )
        return result.returncode, result.stdout, result.stderr
    except FileNotFoundError:
        return -1, "", f"command not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", f"timeout after {timeout}s: {' '.join(cmd)}"


def _check_npm_licenses(root: Path) -> list[dict[str, Any]]:
    """Check npm deps for copyleft licenses using license-checker if available."""
    findings: list[dict[str, Any]] = []
    # Try license-checker (npx)
    rc, stdout, _ = _run_cmd(
        ["npx", "--yes", "license-checker", "--json", "--production"],
        cwd=str(root),
        timeout=120,
    )
    if rc == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return findings
        for pkg, info in data.items():
            licenses_raw = info.get("licenses", "")
            if isinstance(licenses_raw, list):
                licenses_raw = " AND ".join(licenses_raw)
            for copyleft in COPYLEFT_LICENSES:
                if copyleft.lower() in licenses_raw.lower():
                    findings.append({
                        "package": pkg,
                        "license": licenses_raw,
                        "ecosystem": "npm",
                        "manifest": "package.json",
                    })
                    break
    return findings


def _check_python_licenses(root: Path) -> list[dict[str, Any]]:
    """Check Python deps for copyleft licenses using pip-licenses if available."""
    findings: list[dict[str, Any]] = []
    rc, stdout, _ = _run_cmd(
        [sys.executable, "-m", "piplicenses", "--format=json", "--with-system"],
        cwd=str(root),
        timeout=60,
    )
    if rc != 0:
        # fallback: try pip-licenses directly
        rc, stdout, _ = _run_cmd(
            ["pip-licenses", "--format=json", "--with-system"],
            cwd=str(root),
            timeout=60,
        )
    if rc == 0 and stdout.strip():
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            return findings
        for entry in data:
            license_name = entry.get("License", "")
            for copyleft in COPYLEFT_LICENSES:
                if copyleft.lower() in license_name.lower():
                    findings.append({
                        "package": f"{entry.get('Name', '?')}=={entry.get('Version', '?')}",
                        "license": license_name,
                        "ecosystem": "python",
                        "manifest": _find_python_manifest(root),
                    })
                    break
    return findings


def _find_python_manifest(root: Path) -> str:
    """Return the most likely Python manifest file name."""
    for name in ("pyproject.toml", "Pipfile.lock", "poetry.lock", "requirements.txt", "setup.py"):
        if (root / name).exists():
            return name
    return "requirements.txt"


def _check_go_licenses(root: Path) -> list[dict[str, Any]]:
    """Check Go deps for copyleft licenses using go-licenses if available."""
    findings: list[dict[str, Any]] = []
    rc, stdout, _ = _run_cmd(
        ["go-licenses", "report", "./...", "--template", "{{range .}}{{.Name}},{{.LicenseFile}},{{.LicenseName}}\n{{end}}"],
        cwd=str(root),
        timeout=120,
    )
    if rc == 0 and stdout.strip():
        for line in stdout.strip().splitlines():
            parts = line.split(",")
            if len(parts) >= 3:
                pkg, _, license_name = parts[0], parts[1], parts[2]
                for copyleft in COPYLEFT_LICENSES:
                    if copyleft.lower() in license_name.lower():
                        findings.append({
                            "package": pkg,
                            "license": license_name,
                            "ecosystem": "go",
                            "manifest": "go.mod",
                        })
                        break
    return findings


LICENSE_CHECKERS: dict[str, Any] = {
    "npm": _check_npm_licenses,
    "python": _check_python_licenses,
    "go": _check_go_licenses,
}


def check_licenses(root: Path, ecosystems: list[str]) -> list[dict[str, Any]]:
    """Run license checks for all detected ecosystems."""
    all_findings: list[dict[str, Any]] = []
    for eco in ecosystems:
        checker = LICENSE_CHECKERS.get(eco)
        if checker:
            all_findings.extend(checker(root))
    return all_findings


# ---------------------------------------------------------------------------
# (b) SBOM generation
# ---------------------------------------------------------------------------

def generate_sbom(root: Path, task_dir: Path, ecosystems: list[str]) -> dict[str, Any]:
    """Generate SBOM using cyclonedx or syft. Returns status dict."""
    sbom_path = task_dir / "sbom.json"

    # Try syft first (supports all ecosystems)
    rc, stdout, stderr = _run_cmd(
        ["syft", str(root), "-o", "cyclonedx-json"],
        cwd=str(root),
        timeout=300,
    )
    if rc == 0 and stdout.strip():
        sbom_path.write_text(stdout)
        return {"generated": True, "tool": "syft", "path": str(sbom_path), "format": "cyclonedx-json"}

    # Try cyclonedx-bom for Python projects
    if "python" in ecosystems:
        rc, stdout, stderr = _run_cmd(
            [sys.executable, "-m", "cyclonedx_bom", "--format", "json", "-o", str(sbom_path)],
            cwd=str(root),
            timeout=120,
        )
        if rc == 0 and sbom_path.exists():
            return {"generated": True, "tool": "cyclonedx-bom", "path": str(sbom_path), "format": "cyclonedx-json"}

    # Try cyclonedx for npm
    if "npm" in ecosystems:
        rc, stdout, stderr = _run_cmd(
            ["npx", "--yes", "@cyclonedx/cyclonedx-npm", "--output-file", str(sbom_path)],
            cwd=str(root),
            timeout=120,
        )
        if rc == 0 and sbom_path.exists():
            return {"generated": True, "tool": "cyclonedx-npm", "path": str(sbom_path), "format": "cyclonedx-json"}

    return {"generated": False, "tool": None, "path": None, "format": None,
            "reason": "neither syft nor cyclonedx-bom/npm available"}


# ---------------------------------------------------------------------------
# (c) Dependency provenance via Sigstore
# ---------------------------------------------------------------------------

def check_provenance(root: Path, ecosystems: list[str]) -> dict[str, Any]:
    """Check dependency provenance via cosign/Sigstore where available."""
    # cosign must be installed
    rc, _, _ = _run_cmd(["cosign", "version"])
    if rc != 0:
        return {"checked": False, "reason": "cosign not installed"}

    results: list[dict[str, Any]] = []

    # For npm: check npm audit signatures
    if "npm" in ecosystems and (root / "package-lock.json").exists():
        rc, stdout, stderr = _run_cmd(
            ["npm", "audit", "signatures"],
            cwd=str(root),
            timeout=120,
        )
        results.append({
            "ecosystem": "npm",
            "method": "npm audit signatures",
            "passed": rc == 0,
            "detail": stdout.strip()[:500] if rc == 0 else stderr.strip()[:500],
        })

    # For Python: check attestations on PyPI (best-effort via pip)
    if "python" in ecosystems:
        rc, stdout, _ = _run_cmd(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--require-hashes",
             "--no-deps", "--report", "-", "-r", "requirements.txt"],
            cwd=str(root),
            timeout=60,
        )
        results.append({
            "ecosystem": "python",
            "method": "pip --require-hashes (dry-run)",
            "passed": rc == 0,
            "detail": "hash verification passed" if rc == 0 else "hash verification unavailable or failed",
        })

    return {"checked": True, "results": results}


# ---------------------------------------------------------------------------
# (d) Privacy-relevant code checks
# ---------------------------------------------------------------------------

# Patterns that indicate privacy-relevant functionality.
PRIVACY_PATTERNS: dict[str, list[str]] = {
    "data_export": [
        r"export[_-]?data",
        r"download[_-]?data",
        r"data[_-]?export",
        r"gdpr[_-]?export",
        r"user[_-]?data[_-]?download",
        r"takeout",
        r"data[_-]?portability",
    ],
    "account_deletion": [
        r"delete[_-]?account",
        r"account[_-]?delet",
        r"remove[_-]?account",
        r"close[_-]?account",
        r"gdpr[_-]?delet",
        r"right[_-]?to[_-]?erasure",
        r"data[_-]?erasure",
    ],
    "consent_management": [
        r"cookie[_-]?consent",
        r"consent[_-]?manag",
        r"privacy[_-]?preference",
        r"opt[_-]?out",
        r"data[_-]?processing[_-]?consent",
    ],
}

# Indicators that the project handles user data (PII).
PII_INDICATORS = [
    r"user[_-]?email",
    r"password[_-]?hash",
    r"personal[_-]?data",
    r"first[_-]?name.*last[_-]?name",
    r"date[_-]?of[_-]?birth",
    r"social[_-]?security",
    r"phone[_-]?number",
    r"credit[_-]?card",
    r"address.*street",
    r"user[_-]?profile",
    r"session[_-]?token",
    r"auth[_-]?token",
]

# File extensions to scan for privacy patterns.
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".rb", ".java",
    ".kt", ".swift", ".cs", ".php", ".ex", ".exs", ".erl", ".dart",
    ".vue", ".svelte",
}


def _scan_files_for_patterns(root: Path, patterns: list[str], changed_files: list[str] | None = None) -> bool:
    """Return True if any pattern matches in project source files."""
    targets = []
    if changed_files:
        targets = [root / f for f in changed_files if Path(f).suffix in CODE_EXTENSIONS]
    if not targets:
        # Scan up to 500 files to keep it bounded
        count = 0
        for f in root.rglob("*"):
            if f.suffix in CODE_EXTENSIONS and ".dynos" not in f.parts and "node_modules" not in f.parts and ".git" not in f.parts:
                targets.append(f)
                count += 1
                if count >= 500:
                    break

    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
    for fpath in targets:
        try:
            content = fpath.read_text(errors="ignore")
        except (OSError, PermissionError):
            continue
        for pat in compiled:
            if pat.search(content):
                return True
    return False


def check_privacy(root: Path, changed_files: list[str] | None = None) -> dict[str, Any]:
    """Check for missing privacy-relevant code when project handles PII."""
    # First, determine if project handles user data
    handles_pii = _scan_files_for_patterns(root, PII_INDICATORS, changed_files)
    if not handles_pii:
        return {"handles_pii": False, "missing": [], "note": "no PII indicators detected — privacy checks skipped"}

    missing: list[str] = []
    for category, patterns in PRIVACY_PATTERNS.items():
        found = _scan_files_for_patterns(root, patterns)
        if not found:
            missing.append(category)

    return {
        "handles_pii": True,
        "missing": missing,
        "note": f"project handles PII; missing privacy features: {', '.join(missing)}" if missing else "all privacy features detected",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Compliance checks for security-auditor")
    parser.add_argument("--root", required=True, help="Project root directory")
    parser.add_argument("--task-dir", required=True, help="Task directory (.dynos/task-{id})")
    parser.add_argument("--changed-files", help="Comma-separated list of changed files")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    task_dir = Path(args.task_dir).resolve()
    changed_files = args.changed_files.split(",") if args.changed_files else None

    ecosystems = detect_ecosystems(root)

    result: dict[str, Any] = {
        "ecosystems_detected": ecosystems,
        "license_findings": check_licenses(root, ecosystems),
        "sbom": generate_sbom(root, task_dir, ecosystems),
        "provenance": check_provenance(root, ecosystems),
        "privacy": check_privacy(root, changed_files),
    }

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
