"""Entry point for `python -m autofix`."""

from autofix._cli import cli_main
from autofix.scanner import build_parser

raise SystemExit(cli_main(build_parser))
