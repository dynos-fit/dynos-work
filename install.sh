#!/usr/bin/env bash
set -euo pipefail

# dynos-work installer
# Installs the plugin, sets up CLI, registers the project, starts the daemon.

REPO_URL="https://github.com/dynos-fit/dynos-work.git"
INSTALL_DIR="${DYNOS_INSTALL_DIR:-$HOME/.dynos-work}"
BIN_DIR="$INSTALL_DIR/bin"
HOOKS_DIR="$INSTALL_DIR/hooks"
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
    command -v git   >/dev/null 2>&1 || missing+=(git)
    command -v python3 >/dev/null 2>&1 || missing+=(python3)
    if [ ${#missing[@]} -gt 0 ]; then
        fail "Missing required tools: ${missing[*]}"
    fi

    # Optional but recommended
    command -v gh     >/dev/null 2>&1 || warn "gh (GitHub CLI) not found. Autofix PRs and issues will be disabled."
    command -v claude >/dev/null 2>&1 || warn "claude CLI not found. Autofix code changes will be disabled."
}

# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

step_clone() {
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

    if grep -qF "$BIN_DIR" "$SHELL_RC" 2>/dev/null; then
        ok "PATH already configured in $SHELL_RC"
    else
        echo "" >> "$SHELL_RC"
        echo "# dynos-work CLI" >> "$SHELL_RC"
        echo "$path_line" >> "$SHELL_RC"
        ok "Added dynos to PATH in $SHELL_RC"
    fi

    # Make available in current session
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
        warn "Run 'dynos registry register /path/to/project' later to register a project."
    fi
}

step_daemon() {
    local project_dir="${1:-$(pwd)}"
    if [ ! -d "$project_dir/.git" ]; then
        warn "No git repo at $project_dir. Skipping daemon start."
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

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

main() {
    echo ""
    echo "  dynos-work installer"
    echo "  ────────────────────"
    echo ""

    check_deps
    step_clone
    step_path
    step_global_dirs

    # Register and start daemon if in a project directory
    if [ -d "$(pwd)/.git" ]; then
        step_register "$(pwd)"
        step_daemon "$(pwd)"
    fi

    echo ""
    echo "  ────────────────────"
    ok "Installation complete"
    echo ""
    echo "  Quick start:"
    echo ""
    echo "    source $SHELL_RC              # reload PATH (or open new terminal)"
    echo "    dynos local status --root .   # check daemon"
    echo "    dynos global dashboard serve  # start dashboard"
    echo ""
    echo "  In Claude Code:"
    echo ""
    echo "    /dynos-work:start [your task] # start a task"
    echo "    /dynos-work:execute           # execute the plan"
    echo "    /dynos-work:audit             # audit and repair"
    echo ""
    echo "  Full docs: https://github.com/dynos-fit/dynos-work"
    echo ""
}

main "$@"
