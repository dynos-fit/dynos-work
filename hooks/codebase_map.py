#!/usr/bin/env python3
"""Print the codebase map for a planner prompt.

Usage:
  dynos hook codebase_map [--root .] [--max-chars 6000]

Prints one line per folder naming the top-level definitions that already
exist, shared code first, inside the character budget. Prints nothing when
the project has no source files it recognises. No model, no network.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_codemap import DEFAULT_MAP_CHARS, build_map  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=".")
    parser.add_argument("--max-chars", type=int, default=DEFAULT_MAP_CHARS)
    args = parser.parse_args(argv)
    text = build_map(Path(args.root).resolve(), max(200, args.max_chars))
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
