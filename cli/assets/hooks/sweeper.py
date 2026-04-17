#!/usr/bin/env python3
"""Compatibility wrapper — implementation moved to memory/sweeper.py."""
import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
_is_main = __name__ == "__main__"
from memory import sweeper as _real
# Make the real module importable under the old name too
_sys.modules[__name__ if not _is_main else "sweeper"] = _real
if not _is_main:
    _sys.modules["sweeper"] = _real
if _is_main:
    from cli_base import cli_main
    raise SystemExit(cli_main(_real.build_parser))
