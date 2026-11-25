#!/usr/bin/env python3
"""Single-process SessionStart setup for dynos-work.

Replaces 7 sequential Python invocations from hooks/session-start:
  - actor_identity.pin_orchestrator
  - lib_host.detect_host + persist_host
  - registry.set_active (debounced)
  - dashboard.generate (debounced)
  - daemon.ensure (only when auto-enabled / missing)
  - .dynos/routing-context.json generation
  - prints the one-line ROUTING_SUMMARY string to stdout
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))


def _pin_and_persist_host(root: Path, payload_text: str) -> None:
    if payload_text.strip():
        try:
            payload = json.loads(payload_text)
            if isinstance(payload, dict) and str(payload.get("session_id") or "").strip():
                import actor_identity
                actor_identity.pin_orchestrator(root, payload)
        except Exception:
            pass

    try:
        import lib_host
        from lib_core import _persistent_project_dir
        host = lib_host.detect_host()
        targets = [root / ".dynos" / "control-plane.json"]
        try:
            targets.append(_persistent_project_dir(root) / "control-plane.json")
        except Exception:
            pass
        for target in targets:
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                lib_host.persist_host(target, host)
            except Exception:
                pass
    except Exception:
        pass


def _maybe_refresh_registry_and_dashboard(root: Path) -> None:
    try:
        import registry as _reg
        # Debounce registry write to avoid rewriting ~/.dynos/registry.json on every session start
        reg_path = _reg._registry_path() if hasattr(_reg, "_registry_path") else None
        should_set = True
        if reg_path and reg_path.is_file() and (time.time() - reg_path.stat().st_mtime) < 60:
            should_set = False
        if should_set and hasattr(_reg, "cmd_set_active"):
            import argparse
            _reg.cmd_set_active(argparse.Namespace(path=str(root)))
    except Exception:
        pass

    try:
        dash_data = root / ".dynos" / "dashboard-data.json"
        if not dash_data.is_file() or (time.time() - dash_data.stat().st_mtime) >= 300:
            import dashboard as _dash
            if hasattr(_dash, "generate_dashboard"):
                _dash.generate_dashboard(root)
    except Exception:
        pass


def _maybe_ensure_daemon(root: Path) -> None:
    try:
        import daemon as _daemon
        if hasattr(_daemon, "current_pid") and _daemon.current_pid(root):
            return
        # Only auto-spawn daemon when project has already opted into .dynos/ or policy enables it
        if (root / ".dynos" / "daemon.pid").exists() or (root / ".dynos" / "config" / "policy.json").is_file():
            if hasattr(_daemon, "ensure_daemon"):
                _daemon.ensure_daemon(root)
    except Exception:
        pass


def _write_routing_context(root: Path) -> str:
    try:
        from router import load_prevention_rules
        from lib_core import project_policy, _persistent_project_dir
        policy = project_policy(root)
        rules = load_prevention_rules(root)
        registry_path = _persistent_project_dir(root) / "learned-agents" / "registry.json"
        learned_count = 0
        if registry_path.is_file():
            try:
                learned_count = len(json.loads(registry_path.read_text(encoding="utf-8")).get("agents", []))
            except (json.JSONDecodeError, OSError):
                learned_count = 0
        ctx = {
            "policy": {
                k: v
                for k, v in policy.items()
                if k in ("token_budget_multiplier", "fast_track_skip_plan_audit", "model_overrides")
            },
            "prevention_rules": len(rules),
            "learned_agents": learned_count,
            "has_learned_agents": learned_count > 0,
        }
        ctx_path = root / ".dynos" / "routing-context.json"
        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        ctx_path.write_text(json.dumps(ctx, indent=2), encoding="utf-8")

        parts: list[str] = []
        mo = ctx["policy"].get("model_overrides", {})
        if mo:
            parts.append(f"{len(mo)} model overrides active")
        if learned_count:
            parts.append(f"{learned_count} learned agent{'s' if learned_count != 1 else ''} available")
        pr = ctx["prevention_rules"]
        if pr:
            parts.append(f"{pr} prevention rules")
        if ctx["policy"].get("fast_track_skip_plan_audit"):
            parts.append("fast-track plan-audit skip enabled")
        return ". ".join(parts) if parts else "defaults only"
    except Exception:
        return "defaults only"


def _ensure_native_fast_binary() -> None:
    """Compile hooks/bin/dynos-fast lazily when missing or older than native_fast.c."""
    src = _SCRIPT_DIR / "native_fast.c"
    if not src.is_file():
        return
    out = _SCRIPT_DIR / "bin" / "dynos-fast"
    try:
        if out.is_file() and os.access(out, os.X_OK) and out.stat().st_mtime >= src.stat().st_mtime:
            return
        import shutil
        import subprocess
        cc = shutil.which("cc") or shutil.which("clang") or shutil.which("gcc")
        if not cc:
            return
        out.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [cc, "-O3", "-Wall", "-Wextra", "-o", str(out), str(src)],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except Exception:
        pass


def main() -> int:
    root_str = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
    root = Path(root_str).resolve()
    payload_text = sys.stdin.read() if not sys.stdin.isatty() else ""
    _ensure_native_fast_binary()
    _pin_and_persist_host(root, payload_text)
    _maybe_refresh_registry_and_dashboard(root)
    _maybe_ensure_daemon(root)
    summary = _write_routing_context(root)
    sys.stdout.write(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
