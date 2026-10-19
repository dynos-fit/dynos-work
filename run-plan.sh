#!/usr/bin/env bash
# Runs every PR in plan.md unattended, one `claude -p` session per PR.
# PRs are stacked: each branches from the previous one, so later PRs see
# earlier changes without waiting for merges. Merge them on GitHub in order.
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p logs

# landing order and branch names from plan.md
PRS=(
  "01 fix/token-attribution"
  "02 feat/planning-timeline"
  "13 fix/audit-findings-single-source"
  "14 fix/finding-id-namespacing"
  "16 feat/ctl-log-append"
  "15 fix/audit-report-filename-contract"
  "03 feat/auto-role-stamp"
  "05 feat/one-pass-discovery"
  "11 fix/external-gate-triggers"
  "06 fix/risk-calibration"
  "04 refactor/planner-receipt-spawn-log"
  "09 feat/planner-repair-retry-cap"
  "07 feat/combined-planning-path"
  "08 feat/hierarchical-by-subsystem"
  "10 feat/planner-read-budget"
  "17 fix/pre-tool-use-shell-vars"
  "18 fix/gap-analysis-to-be-created"
  "19 fix/claude-md-auditor-hooks-path"
  "20 fix/continuation-requires-receipt"
  "21 fix/git-quotepath"
  "22 fix/amend-plan-revalidate"
  "12 chore/start-phase-budget-guard"
)

git fetch origin main
BASE="origin/main"
BASE_NAME="main"

for entry in "${PRS[@]}"; do
  read -r n branch <<<"$entry"
  echo "=== PR-$n ($branch) from $BASE_NAME ==="
  git checkout -q -B "$branch" "$BASE"

  claude -p "/goal Read plan.md: section 0 and section PR-$n only. You are already on branch $branch, which was created from $BASE_NAME. Implement PR-$n exactly as written. The goal is met when your final output shows all of: the 'python3 -m pytest -q' summary line with exit code 0; the CHANGELOG.md entry you added under Unreleased; 'git push -u origin $branch' succeeded; and the URL printed by 'gh pr create --base $BASE_NAME --fill'. Never edit version fields. Never ask a question; decide, and list decisions in the PR body. If something is blocked, print BLOCKED: and the reason, then stop. Stop after 60 turns." \
    --permission-mode auto \
    --permission-prompts none \
    --max-budget-usd 30 \
    2>&1 | tee "logs/pr-$n.log"

  if grep -q "BLOCKED:" "logs/pr-$n.log"; then
    echo "PR-$n blocked; see logs/pr-$n.log"; exit 1
  fi
  BASE="$branch"; BASE_NAME="$branch"
done
echo "All PRs opened. Merge them on GitHub in the order above."
