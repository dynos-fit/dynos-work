"""Shared CLI entry-point helper for dynos-work hook modules."""

from __future__ import annotations

import argparse
from typing import Callable

from lib_usage_telemetry import record_usage as _record_usage
_record_usage("cli_base")


def cli_main(build_parser_fn: Callable[[], argparse.ArgumentParser]) -> int:
    """Build a parser, parse argv, and dispatch to the handler set via *set_defaults(func=...)*."""
    parser = build_parser_fn()
    args = parser.parse_args()
    return args.func(args)
