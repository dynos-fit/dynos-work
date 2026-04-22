/**
 * TypeScript interfaces for dynos-work dashboard data layer.
 * Matches exact JSON shapes from the API endpoints.
 */

// ---- Task Pipeline ----

export interface TaskClassification {
  type: string;
  domains: string[];
  risk_level: string;
  notes: string;
}

export interface TaskSnapshot {
  head_sha: string;
  branch: string;
}

export interface TaskManifest {
  task_id: string;
  created_at: string;
  title: string;
  raw_input: string;
  input_type: string;
  stage: string;
  classification: TaskClassification;
  fast_track: boolean;
  snapshot?: TaskSnapshot;
  retry_counts: Record<string, number>;
  blocked_reason: string | null;
  completed_at?: string;
  task_dir?: string;
  project_path?: string;
}

// ---- Retrospective / Analytics ----

export interface TaskRetrospective {
  task_id: string;
  task_outcome: string;
  task_type: string;
  task_domains: string[];
  task_risk_level: string;
  findings_by_auditor: Record<string, number>;
  findings_by_category: Record<string, number>;
  executor_repair_frequency: Record<string, number>;
  spec_review_iterations: number;
  repair_cycle_count: number;
  subagent_spawn_count: number;
  wasted_spawns: number;
  auditor_zero_finding_streaks: Record<string, number>;
  executor_zero_repair_streak: Record<string, number>;
  token_usage_by_agent: Record<string, number>;
  total_token_usage: number;
  total_input_tokens: number;
  total_output_tokens: number;
  input_tokens_by_agent: Record<string, number>;
  output_tokens_by_agent: Record<string, number>;
  token_usage_by_model: Record<string, { input_tokens: number; output_tokens: number; tokens: number }>;
  model_used_by_agent: Record<string, string>;
  agent_source: Record<string, string>;
  alongside_overlap: Record<string, unknown>;
  quality_score: number;
  cost_score: number;
  efficiency_score: number;
}

// ---- Execution Graph ----

export interface ExecutionSegment {
  id: string;
  title?: string;
  executor: string;
  depends_on: string[];
  parallelizable: boolean;
  criteria_ids: string[];
  files_expected: string[];
  description: string;
}

export interface ExecutionGraph {
  task_id: string;
  segments: ExecutionSegment[];
}

// ---- Learned Agents ----

export interface BenchmarkSummary {
  sample_count: number;
  mean_quality: number;
  mean_cost: number;
  mean_efficiency: number;
  mean_composite: number;
}

export interface AgentEvaluation {
  evaluated_at: string;
  delta_quality: number;
  delta_composite: number;
  recommendation: string;
  blocked_by_category: string | null;
  fixture_id: string;
  fixture_path: string;
  run_id: string;
  source_tasks: string[];
}

export interface LearnedAgent {
  item_kind: string;
  agent_name: string;
  role: string;
  task_type: string;
  source: string;
  path: string;
  generated_from: string;
  generated_at: string;
  mode: string;
  status: string;
  benchmark_summary?: BenchmarkSummary;
  baseline_summary?: BenchmarkSummary;
  last_evaluation?: AgentEvaluation;
  last_benchmarked_task_offset: number;
  route_allowed: boolean;
  project_path?: string;
}

// ---- Proactive / Autofix ----

export interface ProactiveFinding {
  finding_id: string;
  severity: string;
  category: string;
  description: string;
  evidence: Record<string, unknown>;
  status: string;
  found_at: string;
  processed_at: string | null;
  attempt_count: number;
  pr_number?: number;
  issue_number?: number;
  suppressed_until?: string;
  fail_reason?: string;
  pr_state?: string;
  pr_url?: string;
  merge_outcome?: string;
  merged_at?: string;
  issue_url?: string;
  suppression_reason?: string;
  fixability?: string;
  confidence_score?: number;
  rollout_mode?: string;
  verification?: Record<string, unknown>;
  branch_name?: string;
  pr_quality_score?: number;
}

export interface AutofixCategoryStats {
  mode: string;
  enabled: boolean;
  confidence: number;
  merged: number;
  closed_unmerged: number;
  reverted: number;
  issues_opened: number;
  verification_failed: number;
}

export interface AutofixRateLimits {
  prs_today: number;
  max_prs_per_day: number;
  open_prs: number;
  max_open_prs: number;
}

export interface AutofixTotals {
  findings: number;
  open_prs: number;
  prs_today: number;
  recent_failures: number;
  suppression_count: number;
  merged: number;
  closed_unmerged: number;
  reverted: number;
  issues_opened: number;
}

export interface RecentPR {
  finding_id: string;
  category: string;
  number: number | null;
  state: string;
  merge_outcome: string;
  title: string;
  created_at: string | null;
  url: string | null;
  branch: string | null;
}

export interface AutofixMetrics {
  generated_at: string;
  totals: AutofixTotals;
  rate_limits: AutofixRateLimits;
  categories: Record<string, AutofixCategoryStats>;
  recent_prs: RecentPR[];
}

// ---- Policy / Settings ----

export interface PolicyConfig {
  freshness_task_window: number;
  active_rebenchmark_task_window: number;
  shadow_rebenchmark_task_window: number;
  maintainer_autostart: boolean;
  maintainer_poll_seconds: number;
  fast_track_skip_plan_audit: boolean;
  token_budget_multiplier: number;
}

export interface AutofixCategoryConfig {
  enabled: boolean;
  mode: string;
  min_confidence_autofix?: number;
  confidence?: number;
  stats?: Record<string, unknown>;
}

export interface AutofixPolicyConfig {
  max_prs_per_day: number;
  max_open_prs: number;
  cooldown_after_failures: number;
  allow_dependency_file_changes: boolean;
  suppressions?: Record<string, unknown>;
  categories: Record<string, AutofixCategoryConfig>;
}

// ---- Project Registry ----

export interface ProjectRegistryEntry {
  path: string;
  registered_at: string;
  last_active_at: string;
  status: string;
}

export interface ProjectRegistry {
  version: number;
  projects: ProjectRegistryEntry[];
  checksum: string;
}

// ---- Audit Reports ----

export interface AuditFinding {
  id: string;
  severity: string;
  blocking: boolean;
  category: string;
  title: string;
  description: string;
  location: string;
  evidence: string[];
  recommendation: string;
}

export interface AuditReport {
  auditor_name: string;
  timestamp: string;
  scope: string;
  task_id: string;
  task_type: string;
  files_audited: string[];
  findings: AuditFinding[];
}

// ---- Token / Cost Tracking ----

export interface TokenUsage {
  agents: Record<string, number>;
  by_agent: Record<string, { input_tokens: number; output_tokens: number; tokens: number; model: string }>;
  by_model: Record<string, { input_tokens: number; output_tokens: number; tokens: number }>;
  total: number;
  total_input_tokens: number;
  total_output_tokens: number;
}

export interface CostSummary {
  by_model: Record<string, {
    input_tokens: number;
    output_tokens: number;
    estimated_usd: number;
  }>;
  total_estimated_usd: number;
}

// ---- Repo Analytics ----

export interface RepoState {
  version: number;
  target: string;
  architecture_complexity_score: number;
  dependency_flux: number;
  finding_entropy: number;
  file_count: number;
  line_count: number;
  import_count: number;
  control_flow_count: number;
  dominant_languages: string[];
  recent_findings_by_category: Record<string, number>;
}

export interface RepoProjectStats {
  total_tasks: number;
  task_counts_by_type: Record<string, number>;
  average_quality_score: number;
  executor_reliability: Record<string, number>;
  prevention_rule_frequencies: Record<string, number>;
  prevention_rule_executors: Record<string, string>;
}

export interface RepoSummary {
  learned_components: number;
  active_routes: number;
  shadow_components: number;
  demoted_components: number;
  queued_automation_jobs: number;
  benchmark_runs: number;
  tracked_fixtures: number;
  coverage_gaps: number;
}

export interface RepoActiveRoute {
  agent_name: string;
  role: string;
  task_type: string;
  item_kind: string;
  mode: string;
  composite: number;
}

export interface RepoDemotion {
  agent_name: string;
  role: string;
  task_type: string;
  last_evaluation: Record<string, unknown>;
}

export interface RepoCoverageGap {
  target_name: string;
  role: string;
  task_type: string;
  item_kind: string;
}

export interface RepoBenchmarkRun {
  [key: string]: unknown;
}

export interface RepoReport {
  registry_updated_at: string | null;
  summary: RepoSummary;
  active_routes: RepoActiveRoute[];
  demotions: RepoDemotion[];
  automation_queue: Record<string, unknown>[];
  coverage_gaps: RepoCoverageGap[];
  recent_runs: RepoBenchmarkRun[];
}

// ---- Task Detail: Events ----

export interface TaskEvent {
  ts: string;
  event: string;
  [key: string]: unknown;
}

export interface TaskEventsResponse {
  events: TaskEvent[];
}

export interface WriteBoundaryEvent extends TaskEvent {
  role?: string;
  path?: string;
  operation?: string;
  reason?: string;
  mode?: string;
  wrapper_command?: string;
}

export interface WriteBoundaryPathStat {
  path: string;
  count: number;
}

export interface TaskWriteBoundaryResponse {
  counts: {
    allowed: number;
    wrapper_required: number;
    denied: number;
    total: number;
  };
  by_role: Record<string, number>;
  top_denied_paths: WriteBoundaryPathStat[];
  top_wrapper_paths: WriteBoundaryPathStat[];
  events: WriteBoundaryEvent[];
}

// ---- Task Detail: Receipts ----

export interface TaskReceipt {
  filename: string;
  data: Record<string, unknown>;
}

export interface TaskReceiptsResponse {
  receipts: TaskReceipt[];
}

// ---- Task Detail: Evidence ----

export interface TaskEvidenceFile {
  name: string;
  content: string;
}

export interface TaskEvidenceResponse {
  files: TaskEvidenceFile[];
}

// ---- Task Detail: Completion ----

export interface TaskCompletion {
  files_changed?: string[];
  tests_passed?: number;
  tests_failed?: number;
  audit_result?: string;
  blocking_findings?: number;
  non_blocking_findings?: number;
  [key: string]: unknown;
}

// ---- Task Detail: Postmortem ----

export interface TaskPostmortem {
  json?: Record<string, unknown>;
  markdown?: string;
}

// ---- Task Detail: Router Decisions ----

export interface RouterDecision {
  ts: string;
  event: string;
  role?: string;
  task_type?: string;
  model?: string;
  mode?: string;
  agent_name?: string;
  composite_score?: number;
  source?: string;
  [key: string]: unknown;
}

export interface RouterDecisionsResponse {
  decisions: RouterDecision[];
}

// ---- Task Detail: Markdown Content ----

export interface MarkdownContent {
  content: string;
}

// ---- Maintainer / Control Plane ----

export interface MaintainerStatus {
  updated_at: string;
  running: boolean;
  pid: number;
  poll_seconds: number;
  last_cycle: {
    executed_at: string;
    actions: Array<{ name: string; returncode: number; result?: unknown; stderr?: string }>;
    ok: boolean;
    failed_steps: string[];
    duration_steps: number;
  };
  cycle_count: number;
}

export interface MaintenanceCycle {
  executed_at: string;
  ok: boolean;
  failed_steps: string[];
  duration_steps: number;
  actions: Array<{ name: string; returncode: number; result?: unknown; stderr?: string }>;
}

export interface FreshnessBucket {
  label: string;
  count: number;
  agents: string[];
}

export interface AttentionItem {
  agent_name: string;
  reason: string;
  mode: string;
  status: string;
  recommendation: string | null;
  delta_composite: number | null;
}

export interface ControlPlaneData {
  maintainer: MaintainerStatus;
  autofix_enabled: boolean;
  queue: { version: number; updated_at: string; items: Array<Record<string, unknown>> };
  automation_status: { updated_at: string; queued_before: number; executed: number; pending_after: number };
  agents: LearnedAgent[];
  freshness_buckets: FreshnessBucket[];
  coverage_gaps: RepoCoverageGap[];
  attention_items: AttentionItem[];
  recent_runs: Array<Record<string, unknown>>;
  agent_summary: { total: number; routeable: number; shadow: number; alongside: number; replace: number; demoted: number };
}

// ---- Generic API Response ----

export interface ApiResponse {
  ok: boolean;
  stdout?: string;
  stderr?: string;
}
