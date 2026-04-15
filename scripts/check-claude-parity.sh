#!/usr/bin/env bash
# Parity gate for Claude harness output.
#
# Regenerates a Claude install into a temp directory and compares against the
# frozen fixture at cli/tests/fixtures/claude-parity/.
#
# Fixture semantics:
#   - Pre-tokenization snapshot; holds skills/, agents/, hooks.json,
#     .claude-plugin/plugin.json at its root (NOT under a .claude/ subtree).
#   - The renderer emits a tokenized form, so byte parity is expected for
#     agents/, hooks.json, and plugin.json. Skill bodies legitimately differ
#     by documented tokenization rewrites (${PLUGIN_HOOKS} ->
#     ${CLAUDE_PLUGIN_ROOT}/hooks, --spawn-id args, structured spawn blocks).
#
# Gates (exit 1 on any drift):
#   1. agents/ byte-identical to fixture.
#   2. hooks.json byte-identical to fixture.
#   3. .claude-plugin/plugin.json byte-identical to fixture (version gate
#      subsumed — fixture is pinned at v7.0.0).
#   4. Set of skill names matches fixture (modulo the known execution/
#      restructure).
#   5. Every fixture skill file that referenced ${PLUGIN_HOOKS} has the
#      rewritten idiom ${CLAUDE_PLUGIN_ROOT}/hooks in the regen output.

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
FIXTURE="$REPO/cli/tests/fixtures/claude-parity"
TMPDIR="$(mktemp -d -t dw-parity-XXXXXX)"
trap 'rm -rf "$TMPDIR"' EXIT

export PATH="${HOME}/.bun/bin:$PATH"

if [ ! -d "$FIXTURE" ]; then
  echo "FAIL: fixture missing at $FIXTURE" >&2
  exit 1
fi

echo "[parity] building cli…" >&2
(cd "$REPO/cli" && bun run build >/dev/null)

echo "[parity] regenerating Claude install into $TMPDIR …" >&2
node "$REPO/cli/dist/index.js" init --ai claude --target "$TMPDIR" >/dev/null

REGEN_CLAUDE="$TMPDIR/.claude"
if [ ! -d "$REGEN_CLAUDE" ]; then
  echo "FAIL: regen did not produce $REGEN_CLAUDE" >&2
  exit 1
fi

DRIFT=0

# Gate 1 — agents byte-identical
if ! diff -r -q "$FIXTURE/agents" "$REGEN_CLAUDE/agents" >/tmp/dw-parity-agents.diff 2>&1; then
  DRIFT=1
  echo "DRIFT: agents/ differ from fixture:" >&2
  cat /tmp/dw-parity-agents.diff >&2
fi

# Gate 2 — hooks.json byte-identical
if ! diff -q "$FIXTURE/hooks.json" "$TMPDIR/hooks.json" >/dev/null 2>&1; then
  DRIFT=1
  echo "DRIFT: hooks.json differs from fixture:" >&2
  diff -u "$FIXTURE/hooks.json" "$TMPDIR/hooks.json" >&2 || true
fi

# Gate 3 — .claude-plugin/plugin.json byte-identical
FIXTURE_PLUGIN="$FIXTURE/.claude-plugin/plugin.json"
REGEN_PLUGIN="$TMPDIR/.claude-plugin/plugin.json"
if [ ! -f "$FIXTURE_PLUGIN" ]; then
  DRIFT=1
  echo "DRIFT: fixture missing .claude-plugin/plugin.json" >&2
elif [ ! -f "$REGEN_PLUGIN" ]; then
  DRIFT=1
  echo "DRIFT: regen missing .claude-plugin/plugin.json" >&2
elif ! diff -q "$FIXTURE_PLUGIN" "$REGEN_PLUGIN" >/dev/null 2>&1; then
  DRIFT=1
  echo "DRIFT: plugin.json differs from fixture:" >&2
  diff -u "$FIXTURE_PLUGIN" "$REGEN_PLUGIN" >&2 || true
fi

# Gate 4 — skill file name set matches (modulo execution/ restructure)
FIXTURE_SET="$(cd "$FIXTURE/skills" && find . -type f | grep -v '^\./execution/' | sort)"
REGEN_SET="$(cd "$REGEN_CLAUDE/skills/dynos-work" && find . -type f | grep -v '^\./execution/' | sort)"
if [ "$FIXTURE_SET" != "$REGEN_SET" ]; then
  DRIFT=1
  echo "DRIFT: skill file set differs from fixture:" >&2
  diff <(echo "$FIXTURE_SET") <(echo "$REGEN_SET") >&2 || true
fi

# Gate 5 — every fixture skill referencing ${PLUGIN_HOOKS} must have ${CLAUDE_PLUGIN_ROOT}/hooks in regen
while IFS= read -r rel; do
  if grep -q '\${PLUGIN_HOOKS}' "$FIXTURE/skills/$rel" 2>/dev/null; then
    if [ ! -f "$REGEN_CLAUDE/skills/dynos-work/$rel" ]; then
      DRIFT=1
      echo "DRIFT: fixture skill $rel has no regen counterpart" >&2
      continue
    fi
    if ! grep -q '\${CLAUDE_PLUGIN_ROOT}/hooks' "$REGEN_CLAUDE/skills/dynos-work/$rel"; then
      DRIFT=1
      echo "DRIFT: $rel did not get PLUGIN_HOOKS -> CLAUDE_PLUGIN_ROOT/hooks rewrite" >&2
    fi
  fi
done < <(cd "$FIXTURE/skills" && find . -type f | grep -v '^\./execution/' | sed 's|^\./||')

if [ "$DRIFT" -ne 0 ]; then
  echo "[parity] FAIL: Claude regen drifted from fixture." >&2
  exit 1
fi

echo "[parity] OK: Claude regen matches fixture semantics."
exit 0
