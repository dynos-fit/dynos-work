#!/usr/bin/env bash
set -euo pipefail

# dynos-work installer
# Usage:
#   ./install.sh              # user install (clones to ~/.dynos-work)
#   ./install.sh --develop    # developer install (uses current repo)

REPO_URL="https://github.com/dynos-fit/dynos-work.git"
DEV_MODE=false
INSTALL_DIR=""
BIN_DIR=""
HOOKS_DIR=""
SHELL_RC=""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { printf "\033[1;34m▸\033[0m %s\n" "$*"; }
ok()    { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
warn()  { printf "\033[1;33m!\033[0m %s\n" "$*"; }
fail()  { printf "\033[1;31m✗\033[0m %s\n" "$*"; exit 1; }

detect_shell_rc() {
    if [ -n "${ZSH_VERSION:-}" ] || [ "$(basename "${SHELL:-}")" = "zsh" ]; then
        SHELL_RC="$HOME/.zshrc"
    else
        SHELL_RC="$HOME/.bashrc"
    fi
}

check_deps() {
    local missing=()
    command -v git     >/dev/null 2>&1 || missing+=(git)
    command -v python3 >/dev/null 2>&1 || missing+=(python3)
    if [ ${#missing[@]} -gt 0 ]; then
        fail "Missing required tools: ${missing[*]}"
    fi

    command -v gh     >/dev/null 2>&1 || warn "gh (GitHub CLI) not found. Autofix PRs and issues will be disabled."
    command -v claude >/dev/null 2>&1 || warn "claude CLI not found. Autofix code changes will be disabled."
}

parse_args() {
    for arg in "$@"; do
        case "$arg" in
            --develop|--dev|-d) DEV_MODE=true ;;
            --help|-h)
                echo "Usage: ./install.sh [--develop]"
                echo ""
                echo "  --develop  Developer mode. Uses the current repo directory"
                echo "             instead of cloning to ~/.dynos-work."
                exit 0
                ;;
            *) fail "Unknown argument: $arg. Use --help for usage." ;;
        esac
    done
}

resolve_dirs() {
    # Auto-detect: if running from inside a dynos-work repo, use it
    local script_dir="$(cd "$(dirname "$0")" && pwd)"
    if [ -f "$script_dir/bin/dynos" ] && [ -d "$script_dir/hooks" ]; then
        INSTALL_DIR="$script_dir"
        if [ "$DEV_MODE" = false ]; then
            info "Detected dynos-work repo at $script_dir — using it directly"
        fi
    elif [ "$DEV_MODE" = true ]; then
        fail "Not in a dynos-work repo. Run from the repo root."
    else
        INSTALL_DIR="${DYNOS_INSTALL_DIR:-$HOME/.dynos-work}"
    fi
    BIN_DIR="$INSTALL_DIR/bin"
    HOOKS_DIR="$INSTALL_DIR/hooks"
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

step_clone() {
    if [ "$DEV_MODE" = true ]; then
        ok "Developer mode: using local repo at $INSTALL_DIR"
        return
    fi

    if [ -d "$INSTALL_DIR/.git" ]; then
        info "Updating existing installation at $INSTALL_DIR"
        git -C "$INSTALL_DIR" pull --quiet
        ok "Updated to latest"
    else
        info "Cloning dynos-work to $INSTALL_DIR"
        git clone --quiet "$REPO_URL" "$INSTALL_DIR"
        ok "Cloned"
    fi
}

step_path() {
    detect_shell_rc
    local path_line="export PATH=\"$BIN_DIR:\$PATH\""

    # Remove any old dynos-work PATH entries that point to a different directory
    if grep -q "dynos-work" "$SHELL_RC" 2>/dev/null; then
        # Remove lines with dynos-work CLI comment and old PATH entries
        sed -i "/# dynos-work CLI/d" "$SHELL_RC" 2>/dev/null || true
        sed -i "/dynos-work\/bin/d" "$SHELL_RC" 2>/dev/null || true
    fi

    if grep -qF "$BIN_DIR" "$SHELL_RC" 2>/dev/null; then
        ok "PATH already configured in $SHELL_RC"
    else
        echo "" >> "$SHELL_RC"
        echo "# dynos-work CLI" >> "$SHELL_RC"
        echo "$path_line" >> "$SHELL_RC"
        ok "Added dynos to PATH in $SHELL_RC"
    fi

    export PATH="$BIN_DIR:$PATH"
}

step_global_dirs() {
    mkdir -p "$HOME/.dynos"
    ok "Global state directory ready (~/.dynos)"
}

step_register() {
    local project_dir="${1:-$(pwd)}"
    if [ -d "$project_dir/.git" ]; then
        info "Registering project: $project_dir"
        PYTHONPATH="$HOOKS_DIR:${PYTHONPATH:-}" python3 "$HOOKS_DIR/dynoregistry.py" register "$project_dir" >/dev/null 2>&1 || true
        ok "Project registered"
    else
        warn "Current directory is not a git repo. Skipping project registration."
        warn "Run 'dynos registry register /path/to/project' later."
    fi
}

step_daemon() {
    local project_dir="${1:-$(pwd)}"
    if [ ! -d "$project_dir/.git" ]; then
        warn "No git repo at $project_dir. Skipping daemon start."
        return
    fi

    # Check if already running
    local status
    status=$(PYTHONPATH="$HOOKS_DIR:${PYTHONPATH:-}" python3 "$HOOKS_DIR/dynomaintain.py" status --root "$project_dir" 2>/dev/null) || status='{}'
    if echo "$status" | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('running') else 1)" 2>/dev/null; then
        ok "Local daemon already running"
        return
    fi

    local autofix_flag=""
    if command -v claude >/dev/null 2>&1 && command -v gh >/dev/null 2>&1; then
        autofix_flag="--autofix"
        info "Starting local daemon with autofix (claude + gh detected)"
    else
        info "Starting local daemon (no autofix, missing claude or gh)"
    fi

    PYTHONPATH="$HOOKS_DIR:${PYTHONPATH:-}" python3 "$HOOKS_DIR/dynomaintain.py" start --root "$project_dir" $autofix_flag >/dev/null 2>&1 || true
    ok "Local daemon started"
}

step_plugin() {
    if ! command -v claude >/dev/null 2>&1; then
        warn "claude CLI not found. Skipping plugin install."
        warn "Install the plugin manually in Claude Code: /plugin install dynos-work"
        return
    fi

    # Check if marketplace is added
    if ! claude plugin marketplace list 2>/dev/null | grep -q "dynos-work"; then
        info "Adding dynos-work marketplace"
        claude plugin marketplace add dynos-fit/dynos-work 2>/dev/null || true
    fi

    info "Installing dynos-work plugin"
    if claude plugin install dynos-work 2>/dev/null; then
        ok "Claude Code plugin installed (slash commands ready)"
    else
        warn "Plugin install failed. Install manually in Claude Code: /plugin install dynos-work"
    fi
}

step_global_daemon() {
    # Check if already running
    local status
    status=$(PYTHONPATH="$HOOKS_DIR:${PYTHONPATH:-}" python3 "$HOOKS_DIR/dynoglobal.py" status 2>/dev/null) || status='{}'
    if echo "$status" | python3 -c "import sys,json; sys.exit(0 if json.load(sys.stdin).get('running') else 1)" 2>/dev/null; then
        ok "Global daemon already running"
        return
    fi

    info "Starting global daemon"
    PYTHONPATH="$HOOKS_DIR:${PYTHONPATH:-}" python3 "$HOOKS_DIR/dynoglobal.py" start >/dev/null 2>&1 || true
    ok "Global daemon started (sweeps all registered projects)"
}

step_dev_tests() {
    info "Running tests to verify setup"
    if PYTHONPATH="$HOOKS_DIR:${PYTHONPATH:-}" python3 -m pytest "$INSTALL_DIR/tests/" -x -q 2>&1 | tail -1 | grep -q "passed"; then
        ok "All tests pass"
    else
        warn "Some tests failed. Run 'PYTHONPATH=hooks pytest tests/' for details."
    fi
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    parse_args "$@"
    resolve_dirs

    if [ "$DEV_MODE" = true ]; then
        echo ""
        echo "  dynos-work developer setup"
        echo "  ──────────────────────────"
        echo ""
    else
        echo ""
        echo "  dynos-work installer"
        echo "  ────────────────────"
        echo ""
    fi

    check_deps
    step_clone
    step_path
    step_global_dirs
    step_plugin

    step_global_daemon

    ok "Local daemon not started (manual setup per project)"
    echo ""
    echo "  To set up a project:"
    echo ""
    echo "    cd /path/to/your/project"
    echo "    dynos init              # register + start daemon"
    echo "    dynos init --autofix    # with autofix"

    # Developer extras
    if [ "$DEV_MODE" = true ]; then
        step_dev_tests
    fi

    echo ""
    echo "  ──────────────────────"
    if [ "$DEV_MODE" = true ]; then
        ok "Developer setup complete"
        echo ""
        echo "  Your repo:   $INSTALL_DIR"
        echo "  CLI:         $BIN_DIR/dynos"
        echo "  Hooks:       $HOOKS_DIR/"
        echo "  State:       .dynos/"
        echo "  Global:      ~/.dynos/"
        echo ""
        echo "  Developer commands:"
        echo ""
        echo "    PYTHONPATH=hooks pytest tests/      # run tests"
        echo "    dynos local status --root .         # check daemon"
        echo "    dynos proactive scan --root .       # run autofix scan"
        echo "    dynos global dashboard serve        # start dashboard"
        echo ""
        echo "  Your local changes take effect immediately."
        echo "  No need to reinstall after editing hooks."
    else
        ok "Installation complete"
        echo ""
        echo "  Quick start:"
        echo ""
        echo "    dynos local status --root .   # check daemon"
        echo "    dynos global dashboard serve  # start dashboard"
        echo ""
        echo "  In Claude Code:"
        echo ""
        echo "    /dynos-work:start [your task] # start a task"
        echo "    /dynos-work:execute           # execute the plan"
        echo "    /dynos-work:audit             # audit and repair"
    fi
    echo ""
    echo "  Full docs: https://github.com/dynos-fit/dynos-work"
    echo ""
    echo "  To activate dynos CLI, run:"
    echo ""
    echo "    source $SHELL_RC"
    echo ""
}

main "$@"
