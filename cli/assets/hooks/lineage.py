#!/usr/bin/env python3
"""Compatibility wrapper — implementation moved to telemetry/lineage.py."""
import sys as _sys
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))
_sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
_is_main = __name__ == "__main__"
from telemetry import lineage as _real
_sys.modules[__name__ if not _is_main else "lineage"] = _real
if not _is_main:
    _sys.modules["lineage"] = _real
if _is_main:
    from cli_base import cli_main
    raise SystemExit(cli_main(_real.build_parser))
