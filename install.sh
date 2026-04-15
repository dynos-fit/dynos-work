#!/usr/bin/env bash
set -euo pipefail

# dynos-work installer (7.0.0)
#
# Usage:
#   ./install.sh                  # production install (uses npx dynos-work-cli)
#   ./install.sh --develop        # developer install (uses local cli/dist build)
#   ./install.sh --help           # usage
#
# Behavior:
#   1. Materializes ~/.dynos-work/{bin,hooks}/ (idempotent)
#   2. Delegates skill/agent emission to the dynos-work-cli renderer
#   3. Opt-in, TTY-guarded PATH append to the user's shell-rc (with backup)
#   4. Registers the current project if it's a git repo
#
# Test hooks:
#   DW_INSTALL_TEST_OPTIN=1   # force TTY opt-in branch (tests)

DEV_MODE=false
INSTALL_ROOT=""
BIN_DIR=""
HOOKS_DIR=""
SHELL_RC=""
REPO_ROOT=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { printf "\033[1;34m>\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32mOK\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m!\033[0m %s\n" "$*"; }
fail()  { printf "\033[1;31mX\033[0m %s\n" "$*"; exit 1; }

parse_args() {
    for arg in "$@"; do
        case "$arg" in
            --develop|--dev|-d) DEV_MODE=true ;;
            --help|-h)
                cat <<'USAGE'
Usage: ./install.sh [--develop]

  --develop   Developer mode. Runs the locally-built CLI from cli/dist/index.js
              and copies runtime hooks/bin from the repo checkout instead of
              the npm-distributed assets bundle.

Environment:
  DW_INSTALL_TEST_OPTIN=1   Force TTY opt-in branch (used by tests)
USAGE
                exit 0
                ;;
            *) fail "Unknown argument: $arg. Use --help for usage." ;;
        esac
    done
}

detect_shell_rc() {
    if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
        SHELL_RC="$HOME/.zshrc"
    else
        SHELL_RC="$HOME/.bashrc"
    fi
}

resolve_dirs() {
    INSTALL_ROOT="${DYNOS_INSTALL_DIR:-$HOME/.dynos-work}"
    BIN_DIR="$INSTALL_ROOT/bin"
    HOOKS_DIR="$INSTALL_ROOT/hooks"

    # Script dir is the repo root when invoked as ./install.sh from a checkout
    REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
}

check_deps() {
    local missing=()
    command -v git     >/dev/null 2>&1 || missing+=(git)
    command -v python3 >/dev/null 2>&1 || missing+=(python3)
    command -v node    >/dev/null 2>&1 || missing+=(node)
    if [ ${#missing[@]} -gt 0 ]; then
        fail "Missing required tools: ${missing[*]}"
    fi

    local node_major
    node_major="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
    if [ "$node_major" -lt 18 ]; then
        fail "Node.js >= 18 required (found v$(node -v 2>/dev/null || echo unknown))"
    fi

    command -v claude >/dev/null 2>&1 || warn "claude CLI not found. Claude-native features will be unavailable."
}

# ---------------------------------------------------------------------------
# Runtime materialisation
# ---------------------------------------------------------------------------

copy_tree() {
    local src="$1"
    local dst="$2"
    [ -d "$src" ] || fail "Source tree missing: $src"
    mkdir -p "$dst"
    # Portable idempotent copy. -R preserves structure; existing files are overwritten
    # which is safe for a self-contained runtime dir owned by this installer.
    (cd "$src" && tar cf - .) | (cd "$dst" && tar xf -)
}

step_runtime_copy() {
    mkdir -p "$INSTALL_ROOT"

    local src_hooks="" src_bin=""

    if [ "$DEV_MODE" = true ]; then
        src_hooks="$REPO_ROOT/hooks"
        src_bin="$REPO_ROOT/bin"
        [ -d "$src_hooks" ] || fail "--develop requires $src_hooks. Run from the dynos-work repo root."
        [ -d "$src_bin" ]   || fail "--develop requires $src_bin. Run from the dynos-work repo root."
    else
        # Production: prefer the bundled assets shipped in the npm package.
        # Fall back to repo-root dirs if they exist (useful when running install.sh
        # directly from a cloned checkout without going through npm).
        if [ -d "$REPO_ROOT/cli/assets/hooks" ] && [ -d "$REPO_ROOT/cli/assets/bin" ]; then
            src_hooks="$REPO_ROOT/cli/assets/hooks"
            src_bin="$REPO_ROOT/cli/assets/bin"
        elif [ -d "$REPO_ROOT/hooks" ] && [ -d "$REPO_ROOT/bin" ]; then
            src_hooks="$REPO_ROOT/hooks"
            src_bin="$REPO_ROOT/bin"
        else
            fail "No runtime hooks/bin found. Expected cli/assets/{hooks,bin}/ or repo-root hooks/ and bin/."
        fi
    fi

    info "Materialising runtime at $INSTALL_ROOT"
    copy_tree "$src_hooks" "$HOOKS_DIR"
    copy_tree "$src_bin"   "$BIN_DIR"
    # Ensure the CLI binary is executable
    [ -f "$BIN_DIR/dynos" ] && chmod +x "$BIN_DIR/dynos"
    ok "Runtime ready (~/.dynos-work/{hooks,bin})"
}

# ---------------------------------------------------------------------------
# CLI delegation
# ---------------------------------------------------------------------------

step_cli_delegate() {
    local target="${PWD}"
    if [ "$DEV_MODE" = true ]; then
        local cli_entry="$REPO_ROOT/cli/dist/index.js"
        if [ ! -f "$cli_entry" ]; then
            warn "cli/dist/index.js not found — skipping CLI delegation."
            warn "Build the CLI first: (cd cli && bun install && bun run build)"
            return
        fi
        info "Running local CLI: node cli/dist/index.js init --ai claude --target $target"
        node "$cli_entry" init --ai claude --target "$target" || warn "CLI init exited non-zero"
    else
        if ! command -v npx >/dev/null 2>&1; then
            warn "npx not found — skipping CLI delegation. Install Node.js to enable."
            return
        fi
        info "Running: npx dynos-work-cli init --ai claude --target $target"
        npx --yes dynos-work-cli init --ai claude --target "$target" || warn "CLI init exited non-zero"
    fi
}

# ---------------------------------------------------------------------------
# Shell-rc PATH append (TTY-guarded, opt-in)
# ---------------------------------------------------------------------------

is_tty() {
    [ -t 0 ] && [ -t 1 ]
}

append_path_line() {
    local rc="$1"
    local marker="# dynos-work CLI"
    local path_line='export PATH="$HOME/.dynos-work/bin:$PATH"'

    # Dedupe: strip any previous dynos-work marker+export before re-adding.
    if grep -q "dynos-work CLI" "$rc" 2>/dev/null; then
        # Remove marker lines and any PATH exports that reference the dynos-work bin.
        local tmp
        tmp="$(mktemp)"
        grep -v "# dynos-work CLI" "$rc" | grep -v 'dynos-work/bin' > "$tmp" || true
        mv "$tmp" "$rc"
    fi

    {
        printf '\n'
        printf '%s\n' "$marker"
        printf '%s\n' "$path_line"
    } >> "$rc"
}

step_path() {
    detect_shell_rc
    [ -f "$SHELL_RC" ] || touch "$SHELL_RC"

    # Explicit-consent gate. Only mutate the shell-rc if the user said yes.
    # DW_INSTALL_TEST_OPTIN=1 is a TEST-ONLY override that behaves as if the
    # user answered "y" at the prompt; it exists for the TDD harness and must
    # not be used for unattended production installs.
    local consent="n"
    if [ "${DW_INSTALL_TEST_OPTIN:-0}" = "1" ]; then
        info "DW_INSTALL_TEST_OPTIN=1 — treating as explicit opt-in (test harness override)"
        consent="y"
    elif is_tty; then
        local ans=""
        # shellcheck disable=SC2162
        read -p "Add ~/.dynos-work/bin to PATH in $SHELL_RC? [y/N] " ans || ans=""
        case "$ans" in
            [yY]|[yY][eE][sS]) consent="y" ;;
            *) consent="n" ;;
        esac
    else
        warn "Non-interactive session detected. Skipping PATH edit."
        echo ""
        echo "  To finish setup, add this line to your shell rc:"
        echo ""
        echo "    # dynos-work CLI"
        echo '    export PATH="$HOME/.dynos-work/bin:$PATH"'
        echo ""
        echo "  Or re-run ./install.sh from an interactive terminal."
        return
    fi

    if [ "$consent" != "y" ]; then
        warn "Declined. Skipping PATH edit."
        echo ""
        echo "  To finish setup later, add this line to your shell rc:"
        echo ""
        echo "    # dynos-work CLI"
        echo '    export PATH="$HOME/.dynos-work/bin:$PATH"'
        echo ""
        return
    fi

    # Backup before touching the rc file.
    local backup="${SHELL_RC}.bak-$(date +%s)"
    cp "$SHELL_RC" "$backup"
    info "Backed up $SHELL_RC -> $backup"

    append_path_line "$SHELL_RC"
    ok "Added dynos to PATH in $SHELL_RC"

    export PATH="$BIN_DIR:$PATH"
}

# ---------------------------------------------------------------------------
# Project registration
# ---------------------------------------------------------------------------

step_register() {
    local project_dir="${1:-$(pwd)}"
    if [ ! -d "$project_dir/.git" ]; then
        warn "Current directory is not a git repo. Skipping project registration."
        return
    fi
    if [ ! -f "$HOOKS_DIR/dynoregistry.py" ]; then
        warn "dynoregistry.py not installed. Skipping registration."
        return
    fi
    info "Registering project: $project_dir"
    PYTHONPATH="$HOOKS_DIR:${PYTHONPATH:-}" python3 "$HOOKS_DIR/dynoregistry.py" register "$project_dir" >/dev/null 2>&1 || true
    ok "Project registered"
}

step_global_dirs() {
    mkdir -p "$HOME/.dynos"
    ok "Global state directory ready (~/.dynos)"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"
    resolve_dirs

    echo ""
    if [ "$DEV_MODE" = true ]; then
        echo "  dynos-work developer install (7.0.0)"
    else
        echo "  dynos-work installer (7.0.0)"
    fi
    echo "  ------------------------------------"
    echo ""

    check_deps
    step_runtime_copy
    step_cli_delegate
    step_global_dirs
    step_path
    step_register "$(pwd)"

    echo ""
    echo "  ------------------------------------"
    ok "Installation complete"
    echo ""
    echo "  Install root: $INSTALL_ROOT"
    echo "  CLI binary:   $BIN_DIR/dynos"
    echo ""
    echo "  To activate dynos in this shell, run:"
    echo ""
    if [ -n "$SHELL_RC" ]; then
        echo "    source $SHELL_RC"
    else
        echo '    export PATH="$HOME/.dynos-work/bin:$PATH"'
    fi
    echo ""
    echo "  Docs: https://github.com/dynos-fit/dynos-work"
    echo ""
}

main "$@"
