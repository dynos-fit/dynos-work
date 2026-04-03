---
name: dashboard
description: "Show learned patterns, model/skip policies, quality trends, cost trends, and spawn efficiency from dynos-work retrospectives."
---

# dynos-work: Dashboard

Display a terminal-friendly overview of dynos-work project health, learned patterns, and historical trends.

## What you do

1. Read `dynos_patterns.md` from project memory. Determine the project memory path: `~/.claude/projects/<project>/memory/` where `<project>` is derived from the current working directory by replacing `/` with `-`. Then read `dynos_patterns.md` from that directory.
2. Scan all `.dynos/task-*/task-retrospective.json` files for time-series data.
3. Render a plain-text dashboard to the terminal.

## Data sources

### dynos_patterns.md

Extract the following sections if they exist:
- **Prevention Rules** table: count of rules, list them
- **Model Policy** table: model recommendations per role (may not exist yet)
- **Skip Policy** table: auditor skip thresholds (may not exist yet)
- **Effectiveness Scores** summary (may not exist yet)
- **Spawn Efficiency** metrics: avg spawns, avg wasted, waste ratio
- **Top Finding Categories**: ranked list
- **Executor Reliability**: repair frequency
- **Average Repair Cycles**: by task type

### task-retrospective.json

Each file contains:
- `task_id`, `task_outcome`, `task_type`, `task_domains`, `task_risk_level`
- `findings_by_auditor`: object mapping auditor name to finding count
- `findings_by_category`: object mapping category code to finding count
- `executor_repair_frequency`: object mapping executor to repair count
- `repair_cycle_count`: integer
- `subagent_spawn_count`: integer (may be absent in older retrospectives)
- `wasted_spawns`: integer (may be absent in older retrospectives)
- `auditor_zero_finding_streaks`: object (may be absent)
- `total_token_usage`: integer, total tokens consumed during the task (may be absent in older retrospectives)
- `model_used_by_agent`: string or object mapping agent role to model name (may be absent in older retrospectives)
- `quality_score`: number (0-100), overall quality score for the task (may be absent in older retrospectives)

## Graceful degradation

- If `dynos_patterns.md` does not exist or is empty, print:
  `No learned patterns yet. Run /dynos-work:learn after completing a task.`
- If no `task-retrospective.json` files are found, print:
  `No retrospective data available.`
- If a retrospective JSON file is malformed (parse error), skip it and print a warning line:
  `Warning: Skipping malformed retrospective: {file_path}`
  Never crash on bad JSON.

## Output format

Render the following sections in order. Use plain-text tables and ASCII indicators.

```
dynos-work Dashboard
====================
Generated: {current date/time}
Tasks analyzed: {count of valid retrospectives}

--- Policy Summary ---

Prevention Rules: {count} active
  # | Rule (truncated to 80 chars) | Executor | Category
  --|-------------------------------|----------|----------
  1 | Verify all inline summary...  | refactor | cq
  ...

Model Recommendations:
  {Table from dynos_patterns.md Model Policy section, or "No model policy defined yet."}

Skip Thresholds:
  {Table from dynos_patterns.md Skip Policy section, or "No skip policy defined yet."}

Effectiveness Scores:
  {Summary from dynos_patterns.md, or "No effectiveness data yet."}

--- Quality Trend ---

Findings per task (chronological):

  Task                 | sc | sec | cq | dc | pa | Total | Repairs
  ---------------------|----|----|----|----|----| ------|--------
  task-20260330-001    |  3 |  4 |  6 |  2 |  2 |   17  |    2
  task-20260402-001    |  0 |  6 |  6 |  0 |  0 |   12  |    0
  ...
  Trend                |  v |  ^ |  = |  v |  v |    v  |    v

Trend indicators:  ^ = improving (fewer findings),  v = worsening,  = = stable

--- Token Cost Trend ---

Total token cost per task (chronological):

  Task                 | Tokens
  ---------------------|--------
  task-20260402-001    |  45200
  task-20260402-002    | 128700
  ...
  Trend                |     v

Note: Tasks without total_token_usage are omitted.

--- Model Distribution ---

Model used per task (chronological):

  Task                 | Model(s)
  ---------------------|----------------------------
  task-20260402-001    | claude-opus-4-6
  task-20260402-002    | claude-opus-4-6, claude-sonnet-4
  ...

Note: Tasks without model_used_by_agent are omitted. If value is an object, list unique model names.

--- Quality Score Trend ---

Quality score per task (chronological):

  Task                 | Score
  ---------------------|------
  task-20260402-001    |    72
  task-20260402-002    |    85
  ...
  Trend                |    ^

Note: Tasks without quality_score are omitted. Higher is better, so ^ means improving (higher).

--- Spawn Efficiency (Aggregate) ---

  Metric                  | Value
  ------------------------|------
  Avg spawns per task     | 8.0
  Avg wasted spawns       | 2.5
  Waste ratio             | 31%

--- Top Finding Categories ---

  Rank | Category | Count
  -----|----------|------
  1    | cq       |   19
  2    | sec      |   13
  ...

--- Executor Reliability ---

  Executor          | Repair Tasks
  ------------------|-------------
  refactor-executor |            3

  Avg Repair Cycles by Task Type:
  Task Type | Avg Cycles | Tasks
  ----------|------------|------
  refactor  |        1.0 |     2
  feature   |        0.5 |     2
```

## Trend calculation

For each numeric column across chronologically ordered tasks:
- Compare the last value to the first value.
- If last < first: `^` (improving -- fewer findings/tokens is better). Exception: for `quality_score`, lower is worsening.
- If last > first: `v` (worsening). Exception: for `quality_score`, higher is improving (`^`).
- If last == first: `=` (stable)
- If only one data point: `-` (insufficient data)

## Step-by-step procedure

1. **Read patterns file**: Attempt to read `dynos_patterns.md` from project memory. If the file does not exist or is empty, set a flag `no_patterns = true`.

2. **Parse patterns sections**: If patterns file exists, extract each section by heading:
   - "Prevention Rules" -- parse the markdown table, count rows
   - "Model Policy" -- parse table if present (section may not exist)
   - "Skip Policy" -- parse table if present (section may not exist)
   - "Effectiveness Scores" -- extract summary if present
   - "Spawn Efficiency" -- parse key-value table
   - "Top Finding Categories" -- parse ranked table
   - "Executor Reliability" -- parse table
   - "Average Repair Cycles by Task Type" -- parse table

3. **Scan retrospectives**: Glob for `.dynos/task-*/task-retrospective.json`. For each file:
   - Attempt to read and parse JSON
   - If parse fails, record warning and skip
   - If parse succeeds, add to retrospectives array
   - Sort by `task_id` (lexicographic, which is chronological given the date format)

4. **Compute quality trend**: For each retrospective, extract `findings_by_category` and `repair_cycle_count`. Build the findings-per-task table. Compute trend indicators.

5. **Compute token cost trend**: For retrospectives that have `total_token_usage`, extract the value. Build the token-cost-per-task table. Compute trend indicators (lower is better).

6. **Compute model distribution**: For retrospectives that have `model_used_by_agent`, extract model names. If the value is an object, collect unique model names from the values. Build the model-per-task table.

7. **Compute quality score trend**: For retrospectives that have `quality_score`, extract the value. Build the quality-score-per-task table. Compute trend indicators (higher is better, so `^` means score went up).

8. **Render output**: Print each section using the format above. Use consistent column widths. Right-align numeric columns.

9. **Handle current policy state**: Show Model Recommendations and Skip Thresholds alongside the historical trends so the user sees both current policy and how the system has evolved.
