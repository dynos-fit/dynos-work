#!/usr/bin/env bash
# Renderer drift gate.
#
# This is the externally-facing drift check for dynos-work's multi-harness
# renderer. Functionally it wraps the existing Claude parity gate at
# scripts/check-claude-parity.sh: regenerates a clean Claude install into a
# tmpdir, diffs it against the frozen fixture at cli/tests/fixtures/claude-parity/,
# and exits 1 on any drift.
#
# Kept as a thin wrapper so:
#   - Consumers have a stable, semantically-named entry point
#     (scripts/check-renderer-drift.sh), unambiguous about its purpose.
#   - The underlying parity gate remains owned by seg-006 and can evolve
#     independently without breaking callers.
#
# Criterion 28 (seg-007): "Regenerates + diff -r vs fixture + exit 1 on drift".

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PARITY="$SCRIPT_DIR/check-claude-parity.sh"

if [ ! -x "$PARITY" ]; then
    echo "[drift] check-claude-parity.sh missing or not executable at $PARITY" >&2
    exit 1
fi

echo "[drift] delegating to check-claude-parity.sh" >&2
exec "$PARITY" "$@"
