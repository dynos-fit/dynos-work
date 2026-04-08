"""Shared CLI entry-point helper for the autofix package."""

from __future__ import annotations

import argparse
from typing import Callable


def cli_main(build_parser_fn: Callable[[], argparse.ArgumentParser]) -> int:
    """Build a parser, parse argv, and dispatch to the handler set via *set_defaults(func=...)*."""
    parser = build_parser_fn()
    args = parser.parse_args()
    return args.func(args)
