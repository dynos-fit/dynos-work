---
name: maintain
description: "Autonomous maintenance worker. Periodically scans the repo for technical debt, security anti-patterns, and architectural drift. If issues are found, it automatically creates a repair branch, implements the fix, verifies with audits/tests, and opens a Pull Request."
---

# dynos-work: Maintain (The Autonomous Backend)

Maintains the long-term health of the repository by proactively identifying and resolving issues before they become blocking findings.

## What you do

### Step 1 -- Autonomous Debt Polling (The Trigger)

Every 24 hours (or when invoked), the system performs a **Background Meta-Audit**:
1. Scan the codebase using the **Proactive Meta-Auditor**.
2. Focus on:
   - Dependency vulnerabilities.
   - Architectural drift (deviations from current "Gold Standards").
   - Recurring finding categories from the last 10 tasks.
   - Code-smell clusters identified by complexity metrics.

### Step 2 -- Severity Threshold Gate

For each discovered finding, determine if it meets the **Autonomous Fix Threshold**:
- **Critical (e.g. Auth/Infra):** Immediately proceed to Step 3.
- **High (e.g. Security/Performance):** Proceed to Step 3.
- **Medium/Low:** Append to `.dynos/proactive-findings.json` and wait for the next manual task.

### Step 3 -- The Autonomous Fix Pipeline

If a finding meets the threshold, the system starts an **Auto-Task**:
1. **Branching:** Create a branch `dynos/auto-fix-{finding-id}`.
2. **Implementation:** Use a specialized **Refactor Executor** to implement the fix based on the current "Gold Standard" patterns.
3. **Verification:**
   - Run the full test suite.
   - Spawn a **Security Auditor** (Opus) to verify the fix.
4. **Failure Recovery:** If audits/tests fail, discard the branch and log the failure to `.dynos/maintenance-log.json`. Do not retry more than once.

### Step 4 -- The Hand-off (Pull Request)

If the fix passes ALL audits and tests:
1. **PR Creation:** Open a Pull Request (via `gh pr create` or similar).
2. **Description:** Automatically generate a detailed PR description including:
   - The original finding from the meta-audit.
   - The "Gold Standard" pattern it followed.
   - The audit-pass certificate.
3. **Notify User:** Print:
   ```
   {timestamp} [MAINTAIN] Autonomous PR created: {pr-url} -- fixed {finding-description}
   ```

### Step 5 -- Architectural Strategy Proposals (The Strategist)

If the **Meta-Auditor** identifies a "Debt Cluster" (a module with > 20% of the project's historical findings):

1. **Strategic Refactor Design:** Instead of fixing individual bugs, the maintainer spawns a **Lead Architect (Opus)** to design a clean-slate refactor of the entire module.
2. **Simulation Sandbox Test:**
   - Implement the refactored version in the **Autonomous Simulation Sandbox**.
   - Compare the Quality Score and Audit Pass rate of the *old* logic vs the *new* proposed logic.
3. **The Strategic PR:** If the new version is significantly cleaner/safer, open an **Architectural Proposal PR**. 
   - Report the "Architecture Score" improvement.
   - Include a visual breakdown of the new module's hierarchy.

## Hard Rules
- **Non-Breaking:** If tests fail at any point, immediately abort and revert. Never push code that breaks tests.
- **Monotonicity:** Never push a fix that lowers the overall `quality_score` of the project.
- **Human Gate:** The final merge is always reserved for the human operator via the PR review process.
