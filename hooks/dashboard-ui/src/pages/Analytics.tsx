/**
 * Analytics page — /analytics
 * Scrollable page with 10 Recharts charts powered by GET /api/retrospectives,
 * plus a Costs tab powered by GET /api/cost-summary.
 *
 * Tabs: CHARTS | COSTS
 *
 * Charts tab (10 charts):
 *   1. Quality Trend (LineChart, full width)
 *   2. Cost Score Trend (LineChart, full width)
 *   3. Agent-Model Assignment Mix (PieChart, half width)
 *   4. Executor Repair Frequency (BarChart, half width)
 *   5. Spawn Efficiency (LineChart, full width, dual lines)
 *   6. Token Cost Breakdown (stacked BarChart, full width)
 *   6b. Token I/O per Task (stacked BarChart, input vs output, full width)
 *   7. Findings Per Task (BarChart, full width)
 *   8. Repair Intensity vs Findings (LineChart, full width)
 *   9. Routing Distribution (PieChart, half width — paired with empty half)
 *
 * Costs tab:
 *   - Cost summary table by model
 *   - By-agent token breakdown
 *   - Editable pricing rates with client-side recalculation
 *
 * States: loading (skeleton), empty/insufficient (<2 retros), error (retry), success.
 */

import { useMemo, useState, useCallback } from "react";
import { motion } from "motion/react";
import {
  TrendingUp,
  BarChart3,
  DollarSign,
  Activity,
  GitBranch,
  FileCode2,
  ShieldCheck,
  AlertTriangle,
  FlaskConical,
} from "lucide-react";
import {
  LineChart,
  Line,
  BarChart,
  Bar,
  PieChart,
  Pie,
  Cell,
  XAxis,
  YAxis,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  Legend,
} from "recharts";
import { usePollingData } from "@/data/hooks";
import type {
  TaskManifest,
  TaskRetrospective,
  CostSummary,
  RepoProjectStats,
  RepoReport,
  RepoState,
} from "@/data/types";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { MetricCard } from "@/components/MetricCard";
import { ChartCard } from "@/components/ChartCard";
import { TimeRangeFilter, filterByTimeRange, type TimeRange } from "@/components/TimeRangeFilter";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CHART_HEIGHT = 300;
const PIE_CHART_HEIGHT = 320;

const COLORS = {
  quality: "#BDF000",
  cost: "#B47AFF",
  teal: "#2DD4A8",
  red: "#FF3B3B",
  amber: "#FF9F43",
  pink: "#FF4081",
  lime: "#AEEA00",
  orange: "#FF6D00",
  grid: "#333",
  axisTick: "#999",
  tooltipBg: "#1A1F2E",
  tooltipBorder: "#333",
} as const;

const MODEL_COLORS: Record<string, string> = {
  haiku: "#2DD4A8",
  sonnet: "#BDF000",
  opus: "#B47AFF",
  unknown: "#666",
};

/** Palette for stacked bars — cycles if more agents than colors */
const AGENT_ROLE_COLORS = [
  "#BDF000",
  "#B47AFF",
  "#2DD4A8",
  "#FF4081",
  "#FF9F43",
  "#FF6D00",
  "#AEEA00",
  "#40C4FF",
  "#E040FB",
  "#69F0AE",
];

const ROUTING_COLORS: Record<string, string> = {
  generic: "#666",
  learned: "#BDF000",
};

const AXIS_TICK_STYLE = {
  fill: "#999",
  fontFamily: "JetBrains Mono",
  fontSize: 11,
} as const;

const TOOLTIP_STYLE = {
  backgroundColor: COLORS.tooltipBg,
  border: `1px solid ${COLORS.tooltipBorder}`,
  borderRadius: 8,
  fontFamily: "JetBrains Mono",
  fontSize: 11,
  color: "#ccc",
} as const;

const CHART_MARGIN = {
  top: 8,
  right: 16,
  left: 0,
  bottom: 8,
} as const;

const TASK_X_AXIS_PROPS = {
  tick: AXIS_TICK_STYLE,
  axisLine: { stroke: COLORS.grid },
  tickLine: { stroke: COLORS.grid },
  tickMargin: 8,
  minTickGap: 24,
  interval: "preserveStartEnd" as const,
} as const;

const NA_LABEL = "N/A";

/** Default pricing per 1M tokens (USD) */
const DEFAULT_RATES: Record<string, number> = {
  haiku: 0.25,
  sonnet: 3.0,
  opus: 15.0,
};

// ---------------------------------------------------------------------------
// Data transforms
// ---------------------------------------------------------------------------

function sortByTaskId(retros: TaskRetrospective[]): TaskRetrospective[] {
  return [...retros].sort((a, b) => a.task_id.localeCompare(b.task_id));
}

function shortenTaskId(taskId: string): string {
  const parts = taskId.replace("task-", "").split("-");
  if (parts.length >= 2) {
    const datePart = parts[0];
    const seqPart = parts.slice(1).join("-");
    return `${datePart.slice(-4)}-${seqPart}`;
  }
  return taskId;
}

function shortenLabel(label: string, max = 14): string {
  return label.length > max ? `${label.slice(0, max - 3)}...` : label;
}

interface ModelSlice {
  name: string;
  value: number;
  color: string;
}

function buildModelUsageData(retros: TaskRetrospective[]): ModelSlice[] {
  const counts: Record<string, number> = {};
  for (const retro of retros) {
    if (!retro.model_used_by_agent) continue;
    for (const model of Object.values(retro.model_used_by_agent)) {
      const key = (model as string | null) ?? "unknown";
      counts[key] = (counts[key] || 0) + 1;
    }
  }
  return Object.entries(counts).map(([name, value]) => ({
    name,
    value,
    color: MODEL_COLORS[name] ?? MODEL_COLORS.unknown,
  }));
}

interface RepairEntry {
  executor: string;
  repairs: number;
}

function buildRepairData(retros: TaskRetrospective[]): RepairEntry[] {
  const totals: Record<string, number> = {};
  for (const retro of retros) {
    if (!retro.executor_repair_frequency) continue;
    for (const [executor, count] of Object.entries(retro.executor_repair_frequency)) {
      totals[executor] = (totals[executor] || 0) + count;
    }
  }
  return Object.entries(totals)
    .map(([executor, repairs]) => ({ executor, repairs }))
    .sort((a, b) => b.repairs - a.repairs);
}

// AC-22(a): Token Cost Breakdown — stacked bar chart data
function buildTokenCostData(
  retros: TaskRetrospective[],
): { data: Record<string, string | number>[]; agentKeys: string[] } {
  const agentSet = new Set<string>();
  const data: Record<string, string | number>[] = [];

  for (const retro of retros) {
    const row: Record<string, string | number> = {
      task_id: shortenTaskId(retro.task_id),
    };
    if (retro.token_usage_by_agent && Object.keys(retro.token_usage_by_agent).length > 0) {
      for (const [agent, tokens] of Object.entries(retro.token_usage_by_agent)) {
        agentSet.add(agent);
        row[agent] = tokens;
      }
    }
    data.push(row);
  }

  return { data, agentKeys: Array.from(agentSet).sort() };
}

// AC-22(b): Findings Per Task
interface FindingsEntry {
  task_id: string;
  findings: number | typeof NA_LABEL;
}

function buildFindingsData(retros: TaskRetrospective[]): FindingsEntry[] {
  return retros.map((retro) => {
    if (!retro.findings_by_auditor || Object.keys(retro.findings_by_auditor).length === 0) {
      return { task_id: shortenTaskId(retro.task_id), findings: NA_LABEL };
    }
    const total = Object.values(retro.findings_by_auditor).reduce(
      (sum, val) => sum + val,
      0,
    );
    return { task_id: shortenTaskId(retro.task_id), findings: total };
  });
}

// AC-22(c): Repair Success Rate
interface RepairRateEntry {
  task_id: string;
  success_rate: number | null;
}

function buildRepairRateData(retros: TaskRetrospective[]): RepairRateEntry[] {
  return retros.map((retro) => {
    const totalFindings = retro.findings_by_auditor
      ? Object.values(retro.findings_by_auditor).reduce((s, v) => s + v, 0)
      : 0;
    if (totalFindings === 0 || retro.repair_cycle_count === undefined) {
      return { task_id: shortenTaskId(retro.task_id), success_rate: null };
    }
    // success rate = proportion of findings addressed by repairs
    // clamp to 100% — repairs can exceed findings in iterative cycles
    const rate = Math.min((retro.repair_cycle_count / totalFindings) * 100, 100);
    return { task_id: shortenTaskId(retro.task_id), success_rate: Math.round(rate) };
  });
}

// AC-22(d): Routing Distribution
interface RoutingSlice {
  name: string;
  value: number;
  color: string;
}

function buildRoutingData(retros: TaskRetrospective[]): RoutingSlice[] {
  const counts: Record<string, number> = {};
  for (const retro of retros) {
    if (!retro.agent_source) continue;
    for (const source of Object.values(retro.agent_source)) {
      const key = typeof source === "string" && source.startsWith("learned:")
        ? "learned"
        : "generic";
      counts[key] = (counts[key] || 0) + 1;
    }
  }
  return Object.entries(counts).map(([name, value]) => ({
    name,
    value,
    color: ROUTING_COLORS[name] ?? "#666",
  }));
}

// ---------------------------------------------------------------------------
// Token I/O: input vs output per task (stacked bar)
// ---------------------------------------------------------------------------

interface TokenIOEntry {
  task_id: string;
  input_tokens: number;
  output_tokens: number;
}

function buildTokenIOData(retros: TaskRetrospective[]): TokenIOEntry[] {
  return retros.map((retro) => ({
    task_id: shortenTaskId(retro.task_id),
    input_tokens: retro.total_input_tokens ?? 0,
    output_tokens: retro.total_output_tokens ?? 0,
  }));
}

// ---------------------------------------------------------------------------
// Costs tab: pricing helpers
// ---------------------------------------------------------------------------

interface AgentTokenRow {
  agent: string;
  tokens: number;
}

function buildAgentTokenRows(retros: TaskRetrospective[]): AgentTokenRow[] {
  const totals: Record<string, number> = {};
  for (const retro of retros) {
    if (!retro.token_usage_by_agent) continue;
    for (const [agent, tokens] of Object.entries(retro.token_usage_by_agent)) {
      totals[agent] = (totals[agent] || 0) + tokens;
    }
  }
  return Object.entries(totals)
    .map(([agent, tokens]) => ({ agent, tokens }))
    .sort((a, b) => b.tokens - a.tokens);
}

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatUsd(n: number): string {
  return `$${n.toFixed(4)}`;
}

// ---------------------------------------------------------------------------
// Skeleton placeholders
// ---------------------------------------------------------------------------

function ChartSkeleton({ label }: { label: string }) {
  return (
    <div
      className="rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6"
      role="status"
      aria-label={`Loading ${label}`}
    >
      <Skeleton className="h-4 w-40 mb-4 bg-white/5" />
      <Skeleton className="h-[260px] w-full bg-white/5" />
    </div>
  );
}

function LoadingState() {
  return (
    <div className="space-y-6">
      <ChartSkeleton label="quality trend chart" />
      <ChartSkeleton label="cost trend chart" />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <ChartSkeleton label="model usage chart" />
        <ChartSkeleton label="executor repair chart" />
      </div>
      <ChartSkeleton label="spawn efficiency chart" />
      <ChartSkeleton label="token cost breakdown chart" />
      <ChartSkeleton label="findings per task chart" />
      <ChartSkeleton label="repair success rate chart" />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <ChartSkeleton label="routing distribution chart" />
      </div>
    </div>
  );
}

function CostsLoadingState() {
  return (
    <div className="space-y-6">
      <div
        className="rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6"
        role="status"
        aria-label="Loading cost summary"
      >
        <Skeleton className="h-4 w-48 mb-4 bg-white/5" />
        <Skeleton className="h-[200px] w-full bg-white/5" />
      </div>
      <div
        className="rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6"
        role="status"
        aria-label="Loading pricing rates"
      >
        <Skeleton className="h-4 w-40 mb-4 bg-white/5" />
        <Skeleton className="h-[120px] w-full bg-white/5" />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Empty / Insufficient state
// ---------------------------------------------------------------------------

function InsufficientDataState({ count }: { count: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col items-center justify-center py-20 text-center"
      role="status"
    >
      <BarChart3 className="w-12 h-12 text-slate-600 mb-4" aria-hidden="true" />
      <p className="text-slate-400 font-mono text-sm">
        Insufficient data for charts
      </p>
      <p className="text-slate-600 font-mono text-xs mt-2">
        {count === 0
          ? "No retrospectives found. Complete tasks to generate analytics."
          : "At least 2 completed task retrospectives are required."}
      </p>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Error state
// ---------------------------------------------------------------------------

function ErrorState({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className="flex flex-col items-center justify-center py-20 text-center"
      role="alert"
    >
      <div className="w-12 h-12 rounded-full bg-[#FF3B3B]/10 flex items-center justify-center mb-4">
        <BarChart3 className="w-6 h-6 text-[#FF3B3B]" aria-hidden="true" />
      </div>
      <p className="text-slate-400 font-mono text-sm mb-1">
        Failed to load analytics data
      </p>
      <p className="text-slate-600 font-mono text-xs mb-4">
        {message}
      </p>
      <button
        onClick={onRetry}
        className="px-4 py-2 rounded-lg border border-[#BDF000]/20 text-[#BDF000] font-mono text-xs hover:bg-[#BDF000]/10 transition-colors focus:outline-none focus:ring-2 focus:ring-[#BDF000]/40"
        aria-label="Retry loading analytics data"
      >
        RETRY
      </button>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Summary row helpers (AC-5)
// ---------------------------------------------------------------------------

const BLENDED_RATE_PER_MILLION = 9;

function formatSummaryTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatSummaryCost(n: number): string {
  return `$${n.toFixed(2)}`;
}

function formatPercentage(n: number): string {
  return `${(n * 100).toFixed(1)}%`;
}

function computeTrendPercent(latest: number, priorMean: number): number | null {
  if (priorMean === 0) return null;
  return ((latest - priorMean) / priorMean) * 100;
}

/** Extract a date string from task_id for time-range filtering */
function dateFromTaskId(retro: TaskRetrospective): string | null {
  // task_id format: task-YYYYMMDD-NNN
  const match = retro.task_id.match(/(\d{4})(\d{2})(\d{2})/);
  if (!match) return null;
  return `${match[1]}-${match[2]}-${match[3]}`;
}

// ---------------------------------------------------------------------------
// Costs Tab Content
// ---------------------------------------------------------------------------

function CostsTabContent({
  retros,
}: {
  retros: TaskRetrospective[];
}) {
  const {
    data: costSummary,
    loading: costLoading,
    error: costError,
    refetch: costRefetch,
  } = usePollingData<CostSummary>("/api/cost-summary", 15000);

  const [rates, setRates] = useState<Record<string, number>>({ ...DEFAULT_RATES });

  const handleRateChange = useCallback((model: string, value: string) => {
    const parsed = parseFloat(value);
    if (!isNaN(parsed) && parsed >= 0) {
      setRates((prev) => ({ ...prev, [model]: parsed }));
    }
  }, []);

  const agentRows = useMemo(() => buildAgentTokenRows(retros), [retros]);

  // Compute model-level cost from cost summary + overridden rates
  const modelRows = useMemo(() => {
    if (!costSummary?.by_model) return [];
    return Object.entries(costSummary.by_model).map(([model, info]) => {
      const inputTokens = (info as Record<string, unknown>).input_tokens as number ?? 0;
      const outputTokens = (info as Record<string, unknown>).output_tokens as number ?? 0;
      const totalTokens = (info as Record<string, unknown>).tokens as number ?? (inputTokens + outputTokens);
      const rateKey = Object.keys(rates).find((k) => model.toLowerCase().includes(k));
      const ratePerMillion = rateKey ? rates[rateKey] : 0;
      const estimatedUsd = (totalTokens / 1_000_000) * ratePerMillion;
      return { model, inputTokens, outputTokens, tokens: totalTokens, estimatedUsd };
    });
  }, [costSummary, rates]);

  const totalTokens = useMemo(
    () => modelRows.reduce((s, r) => s + r.tokens, 0),
    [modelRows],
  );
  const totalUsd = useMemo(
    () => modelRows.reduce((s, r) => s + r.estimatedUsd, 0),
    [modelRows],
  );

  if (costLoading) {
    return <CostsLoadingState />;
  }

  if (costError && !costSummary) {
    return <ErrorState message={costError} onRetry={costRefetch} />;
  }

  if (!costSummary || Object.keys(costSummary.by_model ?? {}).length === 0) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        className="flex flex-col items-center justify-center py-20 text-center"
        role="status"
      >
        <DollarSign className="w-12 h-12 text-slate-600 mb-4" aria-hidden="true" />
        <p className="text-slate-400 font-mono text-sm">
          No cost data available
        </p>
        <p className="text-slate-600 font-mono text-xs mt-2">
          Cost data will appear after tasks generate token usage metrics.
        </p>
      </motion.div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      className="space-y-6"
    >
      {/* Cost by Model table */}
      <div className="rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6 card-hover-glow">
        <div className="flex items-center gap-2 mb-4">
          <DollarSign className="w-4 h-4 text-[#BDF000]" aria-hidden />
          <h3 className="font-mono text-xs font-semibold text-slate-300 tracking-wider uppercase">
            Cost by Model
          </h3>
        </div>
        <div className="overflow-x-auto">
          <table className="w-full font-mono text-xs" aria-label="Cost breakdown by model">
            <thead>
              <tr className="border-b border-white/10">
                <th className="text-left text-slate-500 py-2 pr-4">Model</th>
                <th className="text-right text-slate-500 py-2 pr-4">Input</th>
                <th className="text-right text-slate-500 py-2 pr-4">Output</th>
                <th className="text-right text-slate-500 py-2 pr-4">Total</th>
                <th className="text-right text-slate-500 py-2">Est. USD</th>
              </tr>
            </thead>
            <tbody>
              {modelRows.map((row) => (
                <tr key={row.model} className="border-b border-white/5">
                  <td className="text-slate-300 py-2 pr-4">{row.model}</td>
                  <td className="text-right text-[#B47AFF] py-2 pr-4">
                    {formatTokens(row.inputTokens)}
                  </td>
                  <td className="text-right text-[#BDF000] py-2 pr-4">
                    {formatTokens(row.outputTokens)}
                  </td>
                  <td className="text-right text-slate-400 py-2 pr-4">
                    {formatTokens(row.tokens)}
                  </td>
                  <td className="text-right text-[#BDF000] py-2">
                    {formatUsd(row.estimatedUsd)}
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-[#BDF000]/20">
                <td className="text-slate-300 font-semibold py-2 pr-4">Total</td>
                <td className="text-right text-[#B47AFF] font-semibold py-2 pr-4">
                  {formatTokens(modelRows.reduce((s, r) => s + r.inputTokens, 0))}
                </td>
                <td className="text-right text-[#BDF000] font-semibold py-2 pr-4">
                  {formatTokens(modelRows.reduce((s, r) => s + r.outputTokens, 0))}
                </td>
                <td className="text-right text-slate-300 font-semibold py-2 pr-4">
                  {formatTokens(totalTokens)}
                </td>
                <td className="text-right text-[#BDF000] font-semibold py-2">
                  {formatUsd(totalUsd)}
                </td>
              </tr>
            </tfoot>
          </table>
        </div>
      </div>

      {/* By-agent breakdown */}
      <div className="rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6 card-hover-glow">
        <div className="flex items-center gap-2 mb-4">
          <Activity className="w-4 h-4 text-[#BDF000]" aria-hidden />
          <h3 className="font-mono text-xs font-semibold text-slate-300 tracking-wider uppercase">
            Tokens by Agent
          </h3>
        </div>
        {agentRows.length === 0 ? (
          <p className="text-slate-600 font-mono text-xs">
            No per-agent token data available.
          </p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full font-mono text-xs" aria-label="Token usage by agent">
              <thead>
                <tr className="border-b border-white/10">
                  <th className="text-left text-slate-500 py-2 pr-4">Agent</th>
                  <th className="text-right text-slate-500 py-2">Tokens</th>
                </tr>
              </thead>
              <tbody>
                {agentRows.map((row) => (
                  <tr key={row.agent} className="border-b border-white/5">
                    <td className="text-slate-300 py-2 pr-4 max-w-[200px] truncate" title={row.agent}>
                      {row.agent}
                    </td>
                    <td className="text-right text-slate-400 py-2">
                      {formatTokens(row.tokens)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Editable pricing rates */}
      <div className="rounded-xl border border-[#BDF000]/10 bg-[#0D1117]/60 p-6 card-hover-glow">
        <div className="flex items-center gap-2 mb-4">
          <DollarSign className="w-4 h-4 text-[#B47AFF]" aria-hidden />
          <h3 className="font-mono text-xs font-semibold text-slate-300 tracking-wider uppercase">
            Pricing Rates ($/1M tokens)
          </h3>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          {Object.entries(rates).map(([model, rate]) => (
            <div key={model}>
              <label
                htmlFor={`rate-${model}`}
                className="block text-slate-500 font-mono text-xs mb-1 uppercase tracking-wider"
              >
                {model}
              </label>
              <input
                id={`rate-${model}`}
                type="number"
                min="0"
                step="0.01"
                value={rate}
                onChange={(e) => handleRateChange(model, e.target.value)}
                className="w-full bg-black/40 border border-white/10 text-slate-200 p-2 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded-none"
                aria-label={`Pricing rate for ${model} in USD per million tokens`}
              />
            </div>
          ))}
        </div>
        <p className="text-slate-600 font-mono text-[10px] mt-4 tracking-wider uppercase">
          Estimates based on default pricing
        </p>
      </div>
    </motion.div>
  );
}

function formatCompactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return `${n}`;
}

function formatDecimal(n: number, digits = 3): string {
  return n.toFixed(digits);
}

function sortEntriesDescending(record: Record<string, number>): Array<[string, number]> {
  return Object.entries(record).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function DataTable({
  headers,
  rows,
  empty,
}: {
  headers: string[];
  rows: Array<Array<string | number>>;
  empty: string;
}) {
  if (rows.length === 0) {
    return <p className="text-slate-600 font-mono text-xs py-8 text-center">{empty}</p>;
  }

  return (
    <div className="overflow-x-auto">
      <table className="w-full font-mono text-xs">
        <thead>
          <tr className="border-b border-white/10">
            {headers.map((header) => (
              <th key={header} className="text-left text-slate-500 py-2 pr-4 uppercase tracking-wider">
                {header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, rowIndex) => (
            <tr key={`${row.join("-")}-${rowIndex}`} className="border-b border-white/5">
              {row.map((cell, cellIndex) => (
                <td
                  key={`${rowIndex}-${cellIndex}`}
                  className={`py-2 pr-4 ${cellIndex === row.length - 1 ? "text-slate-300" : "text-slate-400"}`}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RepoTabContent({
  report,
  state,
  stateError,
  projectStats,
  totalTaskCount,
}: {
  report: RepoReport | null;
  state: RepoState | null;
  stateError: string | null;
  projectStats: RepoProjectStats | null;
  totalTaskCount: number;
}) {
  const taskTypeRows = useMemo(
    () => sortEntriesDescending(projectStats?.task_counts_by_type ?? {}).map(([taskType, count]) => [taskType, formatCompactNumber(count)]),
    [projectStats],
  );
  const executorRows = useMemo(
    () => sortEntriesDescending(projectStats?.executor_reliability ?? {}).map(([executor, score]) => [executor, `${(score * 100).toFixed(1)}%`]),
    [projectStats],
  );
  const preventionRows = useMemo(
    () => sortEntriesDescending(projectStats?.prevention_rule_frequencies ?? {})
      .slice(0, 8)
      .map(([rule, count]) => [
        rule.length > 80 ? `${rule.slice(0, 77)}...` : rule,
        projectStats?.prevention_rule_executors?.[rule] ?? "unknown",
        count,
      ]),
    [projectStats],
  );
  const routeRows = useMemo(
    () => [...(report?.active_routes ?? [])]
      .sort((a, b) => b.composite - a.composite)
      .slice(0, 8)
      .map((route) => [route.agent_name, route.role, route.task_type, route.composite.toFixed(3)]),
    [report],
  );
  const demotionRows = useMemo(
    () => (report?.demotions ?? [])
      .slice(0, 8)
      .map((demotion) => {
        const delta = typeof demotion.last_evaluation?.delta_composite === "number"
          ? demotion.last_evaluation.delta_composite.toFixed(3)
          : "n/a";
        return [demotion.agent_name, demotion.role, demotion.task_type, delta];
      }),
    [report],
  );
  const gapRows = useMemo(
    () => (report?.coverage_gaps ?? []).slice(0, 8).map((gap) => [gap.target_name, gap.role, gap.task_type, gap.item_kind]),
    [report],
  );
  const recentRunRows = useMemo(
    () => (report?.recent_runs ?? []).slice().reverse().map((run, index) => [
      typeof run.run_id === "string" ? run.run_id : `run-${index + 1}`,
      typeof run.fixture_id === "string" ? run.fixture_id : "n/a",
      typeof run.timestamp === "string"
        ? run.timestamp
        : typeof run.completed_at === "string"
          ? run.completed_at
          : "n/a",
      typeof run.outcome === "string"
        ? run.outcome
        : typeof run.recommendation === "string"
          ? run.recommendation
          : "n/a",
    ]),
    [report],
  );
  const findingCategoryRows = useMemo(
    () => sortEntriesDescending(state?.recent_findings_by_category ?? {}).map(([category, count]) => [category, count]),
    [state],
  );

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <MetricCard
          label="Total Tasks"
          value={formatCompactNumber(totalTaskCount)}
          icon={<ShieldCheck className="w-3.5 h-3.5 text-[#BDF000]" aria-hidden="true" />}
        />
        <MetricCard
          label="Active Routes"
          value={formatCompactNumber(report?.summary.active_routes ?? 0)}
          icon={<GitBranch className="w-3.5 h-3.5 text-[#2DD4A8]" aria-hidden="true" />}
        />
        <MetricCard
          label="Tracked Fixtures"
          value={formatCompactNumber(report?.summary.tracked_fixtures ?? 0)}
          icon={<FlaskConical className="w-3.5 h-3.5 text-[#B47AFF]" aria-hidden="true" />}
        />
        <MetricCard
          label="Code Files"
          value={formatCompactNumber(state?.file_count ?? 0)}
          icon={<FileCode2 className="w-3.5 h-3.5 text-[#FF9F43]" aria-hidden="true" />}
        />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard
          title="Repo Runtime Summary"
          subtitle="Learned routes, benchmark coverage, queue pressure, and registry freshness."
        >
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Learned Components</div>
              <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatCompactNumber(report?.summary.learned_components ?? 0)}</div>
            </div>
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Shadow Components</div>
              <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatCompactNumber(report?.summary.shadow_components ?? 0)}</div>
            </div>
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Benchmark Runs</div>
              <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatCompactNumber(report?.summary.benchmark_runs ?? 0)}</div>
            </div>
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Automation Queue</div>
              <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatCompactNumber(report?.summary.queued_automation_jobs ?? 0)}</div>
            </div>
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Coverage Gaps</div>
              <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatCompactNumber(report?.summary.coverage_gaps ?? 0)}</div>
            </div>
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Registry Updated</div>
              <div className="text-xs font-mono text-slate-300 mt-2 break-all">
                {report?.registry_updated_at ?? "n/a"}
              </div>
            </div>
          </div>
        </ChartCard>

        <ChartCard
          title="Codebase State"
          subtitle="Structural complexity, dependency flux, code volume, and recent finding density."
        >
          {state ? (
            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Architecture Complexity</div>
                <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatDecimal(state.architecture_complexity_score, 2)}</div>
              </div>
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Dependency Flux</div>
                <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatDecimal(state.dependency_flux, 2)}</div>
              </div>
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Finding Entropy</div>
                <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatDecimal(state.finding_entropy, 2)}</div>
              </div>
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Line Count</div>
                <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatCompactNumber(state.line_count)}</div>
              </div>
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Import Count</div>
                <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatCompactNumber(state.import_count)}</div>
              </div>
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em]">Control Flow Count</div>
                <div className="text-xl font-bold text-[#F0F0E8] mt-2">{formatCompactNumber(state.control_flow_count)}</div>
              </div>
              <div className="col-span-full rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-[#7A776E] uppercase tracking-[0.12em] mb-3">Dominant Languages</div>
                <div className="flex flex-wrap gap-2">
                  {state.dominant_languages.length > 0 ? state.dominant_languages.map((language) => (
                    <span key={language} className="rounded-full border border-[#BDF000]/20 bg-[#BDF000]/10 px-3 py-1 text-[11px] font-mono text-[#D8E7A0]">
                      {language}
                    </span>
                  )) : (
                    <span className="text-slate-600 font-mono text-xs">No code files indexed.</span>
                  )}
                </div>
              </div>
            </div>
          ) : (
            <div className="flex items-start gap-3 rounded-xl border border-[#FF9F43]/20 bg-[#FF9F43]/5 p-4">
              <AlertTriangle className="w-4 h-4 text-[#FF9F43] mt-0.5" aria-hidden="true" />
              <div>
                <div className="text-xs font-mono text-slate-300">Repo state unavailable</div>
                <div className="text-[11px] font-mono text-slate-500 mt-1">
                  {stateError ?? "State metrics are unavailable for the current selection."}
                </div>
              </div>
            </div>
          )}
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard title="Task Mix" subtitle="Retrospective-backed mix of completed tasks, not all task manifests.">
          <DataTable headers={["Task Type", "Count"]} rows={taskTypeRows} empty="No task type stats available." />
        </ChartCard>

        <ChartCard title="Executor Reliability" subtitle="Retrospective-derived signal based on repair frequency for completed tasks.">
          <DataTable headers={["Executor", "Reliability"]} rows={executorRows} empty="No executor reliability stats available." />
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard title="Active Routes" subtitle="Highest-performing routeable learned components by composite benchmark score.">
          <DataTable headers={["Agent", "Role", "Task Type", "Composite"]} rows={routeRows} empty="No active routes available." />
        </ChartCard>

        <ChartCard title="Coverage Gaps" subtitle="Shadow components that do not have benchmark fixtures yet.">
          <DataTable headers={["Target", "Role", "Task Type", "Kind"]} rows={gapRows} empty="No coverage gaps detected." />
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard title="Demotions" subtitle="Recently demoted components and their last recorded composite delta.">
          <DataTable headers={["Agent", "Role", "Task Type", "Delta"]} rows={demotionRows} empty="No demotions recorded." />
        </ChartCard>

        <ChartCard title="Recent Benchmark Runs" subtitle="Most recent benchmark activity visible to the runtime report.">
          <DataTable headers={["Run", "Fixture", "Timestamp", "Outcome"]} rows={recentRunRows} empty="No benchmark runs recorded." />
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard title="Recent Findings by Category" subtitle="Aggregated from the last five retrospectives used by repo state encoding.">
          <DataTable headers={["Category", "Findings"]} rows={findingCategoryRows} empty="No recent finding categories available." />
        </ChartCard>

        <ChartCard title="Prevention Rules" subtitle="Most frequent prevention rules extracted from completed-task retrospectives.">
          <DataTable headers={["Rule", "Executor", "Count"]} rows={preventionRows} empty="No prevention rules recorded." />
        </ChartCard>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function Analytics() {
  const {
    data: tasks,
    loading: tasksLoading,
    error: tasksError,
    refetch: refetchTasks,
  } = usePollingData<TaskManifest[]>("/api/tasks", 10000);
  const { data, loading, error, refetch } = usePollingData<TaskRetrospective[]>(
    "/api/retrospectives",
    10000,
  );
  const {
    data: repoReport,
    loading: reportLoading,
    error: reportError,
    refetch: refetchReport,
  } = usePollingData<RepoReport>("/api/report", 15000);
  const {
    data: repoState,
    loading: stateLoading,
    error: stateError,
    refetch: refetchState,
  } = usePollingData<RepoState>("/api/state", 15000);
  const {
    data: projectStats,
    loading: statsLoading,
    error: statsError,
    refetch: refetchStats,
  } = usePollingData<RepoProjectStats>("/api/project-stats", 15000);

  // Time range filter state (AC-7)
  const [timeRange, setTimeRange] = useState<TimeRange>("All");

  // Sort chronologically once
  const sorted = useMemo(() => (data ? sortByTaskId(data) : []), [data]);

  // Time-filtered subset for charts (AC-7)
  const filtered = useMemo(
    () => filterByTimeRange(sorted, dateFromTaskId, timeRange),
    [sorted, timeRange],
  );

  // ---- AC-5: Summary row metrics (computed from ALL data, not filtered) ----
  const summaryMetrics = useMemo(() => {
    if (!tasks && !projectStats && !repoReport && !repoState && sorted.length === 0) return null;

    const totalTasks = tasks?.length ?? projectStats?.total_tasks ?? sorted.length;
    const avgQualityScore = projectStats?.average_quality_score
      ?? (sorted.length > 0
        ? sorted.reduce((sum, retro) => sum + (retro.quality_score ?? 0), 0) / sorted.length
        : 0);
    const activeRoutes = repoReport?.summary.active_routes ?? 0;
    const queueDepth = repoReport?.summary.queued_automation_jobs ?? 0;
    const fileCount = repoState?.file_count ?? 0;
    const totalTokens = sorted.reduce((sum, retro) => sum + (retro.total_token_usage ?? 0), 0);
    const estCost = (totalTokens / 1_000_000) * BLENDED_RATE_PER_MILLION;

    const latest = sorted[sorted.length - 1];
    const prior = sorted.slice(0, -1);
    const priorMeanQuality = prior.length > 0
      ? prior.reduce((sum, retro) => sum + (retro.quality_score ?? 0), 0) / prior.length
      : 0;
    const priorMeanRepairs = prior.length > 0
      ? prior.reduce((sum, retro) => sum + (retro.repair_cycle_count ?? 0), 0) / prior.length
      : 0;
    const latestRepairs = latest?.repair_cycle_count ?? 0;

    return {
      totalTasks: formatCompactNumber(totalTasks),
      avgQuality: formatPercentage(avgQualityScore),
      activeRoutes: formatCompactNumber(activeRoutes),
      fileCount: formatCompactNumber(fileCount),
      queueDepth: formatCompactNumber(queueDepth),
      estCost: formatSummaryCost(estCost),
      trendQuality: latest ? computeTrendPercent(latest.quality_score ?? 0, priorMeanQuality) : null,
      trendRepairs: latest ? computeTrendPercent(latestRepairs, priorMeanRepairs) : null,
    };
  }, [tasks, projectStats, repoReport, repoState, sorted]);

  // Derived chart data — uses filtered subset
  const qualityData = useMemo(
    () => filtered.map((r) => ({ task_id: shortenTaskId(r.task_id), quality_score: r.quality_score })),
    [filtered],
  );

  const costData = useMemo(
    () => filtered.map((r) => ({ task_id: shortenTaskId(r.task_id), cost_score: r.cost_score })),
    [filtered],
  );

  const modelData = useMemo(() => buildModelUsageData(filtered), [filtered]);

  const repairData = useMemo(() => buildRepairData(filtered), [filtered]);

  const spawnData = useMemo(
    () =>
      filtered.map((r) => ({
        task_id: shortenTaskId(r.task_id),
        total_spawns: r.subagent_spawn_count,
        wasted_spawns: r.wasted_spawns,
      })),
    [filtered],
  );

  // AC-22: New chart data
  const tokenCostResult = useMemo(() => buildTokenCostData(filtered), [filtered]);
  const findingsData = useMemo(() => buildFindingsData(filtered), [filtered]);
  const repairRateData = useMemo(() => buildRepairRateData(filtered), [filtered]);
  const routingData = useMemo(() => buildRoutingData(filtered), [filtered]);
  const tokenIOData = useMemo(() => buildTokenIOData(filtered), [filtered]);

  // TimeRangeFilter element reused across chart cards
  const timeRangeAction = (
    <TimeRangeFilter value={timeRange} onChange={setTimeRange} />
  );

  // ---- Render states ----

  const pageHeader = (
    <div className="flex items-center gap-3 mb-6">
      <BarChart3 className="w-5 h-5 text-[#BDF000]" aria-hidden="true" />
      <h1 className="font-mono text-sm font-semibold text-[#BDF000] tracking-widest uppercase">
        Analytics
      </h1>
    </div>
  );

  if (tasksLoading && loading && reportLoading && statsLoading && stateLoading) {
    return (
      <div className="p-4 sm:p-6 max-w-7xl mx-auto">
        {pageHeader}
        <LoadingState />
      </div>
    );
  }

  if (tasksError && !tasks && error && !data && reportError && !repoReport && statsError && !projectStats) {
    return (
      <div className="p-4 sm:p-6 max-w-7xl mx-auto">
        {pageHeader}
        <ErrorState
          message={[tasksError, error, reportError, statsError].filter(Boolean).join(" | ")}
          onRetry={() => {
            refetchTasks();
            refetch();
            refetchReport();
            refetchStats();
            refetchState();
          }}
        />
      </div>
    );
  }

  return (
    <div className="p-4 sm:p-6 max-w-7xl mx-auto">
      {pageHeader}

      {/* AC-5: Summary row */}
      {summaryMetrics && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6" role="region" aria-label="Summary metrics">
          <MetricCard
            label="Total Tasks"
            value={summaryMetrics.totalTasks}
            icon={<ShieldCheck className="w-3.5 h-3.5 text-[#BDF000]" aria-hidden="true" />}
            delay={0}
          />
          <MetricCard
            label="Avg Quality (Completed)"
            value={summaryMetrics.avgQuality}
            trend={summaryMetrics.trendQuality}
            trendLabel="latest vs prior mean"
            icon={<TrendingUp className="w-3.5 h-3.5 text-[#2DD4A8]" aria-hidden="true" />}
            delay={0.05}
          />
          <MetricCard
            label="Active Routes"
            value={summaryMetrics.activeRoutes}
            icon={<GitBranch className="w-3.5 h-3.5 text-[#B47AFF]" aria-hidden="true" />}
            delay={0.1}
          />
          <MetricCard
            label="Code Files"
            value={summaryMetrics.fileCount}
            icon={<FileCode2 className="w-3.5 h-3.5 text-[#FF9F43]" aria-hidden="true" />}
            delay={0.15}
          />
        </div>
      )}

      <Tabs defaultValue="charts">
        <TabsList className="bg-[#0D1117]/80 border border-[#BDF000]/10 mb-6">
          <TabsTrigger
            value="charts"
            className="font-mono text-xs tracking-wider uppercase data-[state=active]:text-[#BDF000] data-[state=active]:bg-[#BDF000]/10"
            aria-label="View charts"
          >
            Charts
          </TabsTrigger>
          <TabsTrigger
            value="costs"
            className="font-mono text-xs tracking-wider uppercase data-[state=active]:text-[#BDF000] data-[state=active]:bg-[#BDF000]/10"
            aria-label="View cost analysis"
          >
            Costs
          </TabsTrigger>
          <TabsTrigger
            value="repo"
            className="font-mono text-xs tracking-wider uppercase data-[state=active]:text-[#BDF000] data-[state=active]:bg-[#BDF000]/10"
            aria-label="View repo analytics"
          >
            Repo
          </TabsTrigger>
        </TabsList>

        <TabsContent value="charts">
          {!data || data.length < 2 ? (
            <InsufficientDataState count={data?.length ?? 0} />
          ) : (
          <div className="space-y-6">
            {/* Chart 1: Quality Trend (full width) */}
            <ChartCard title="Quality Trend" action={timeRangeAction}>
              <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
                <LineChart data={qualityData} style={{ background: "transparent" }} margin={CHART_MARGIN}>
                  <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
                  <XAxis
                    dataKey="task_id"
                    {...TASK_X_AXIS_PROPS}
                  />
                  <YAxis
                    domain={[0, 1]}
                    tick={AXIS_TICK_STYLE}
                    axisLine={{ stroke: COLORS.grid }}
                    tickLine={{ stroke: COLORS.grid }}
                  />
                  <Tooltip contentStyle={TOOLTIP_STYLE} />
                  <Line
                    type="monotone"
                    dataKey="quality_score"
                    stroke={COLORS.quality}
                    strokeWidth={2}
                    dot={{ r: 4, fill: COLORS.quality }}
                    activeDot={{ r: 6 }}
                    name="Quality Score"
                  />
                </LineChart>
              </ResponsiveContainer>
            </ChartCard>

            {/* Chart 2: Cost Score Trend (full width) */}
            <ChartCard
              title="Cost Score Trend"
              subtitle="Dynos retrospective cost score, not dollar spend."
              action={timeRangeAction}
            >
              <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
                <LineChart data={costData} style={{ background: "transparent" }} margin={CHART_MARGIN}>
                  <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
                  <XAxis
                    dataKey="task_id"
                    {...TASK_X_AXIS_PROPS}
                  />
                  <YAxis
                    domain={[0, 1]}
                    tick={AXIS_TICK_STYLE}
                    axisLine={{ stroke: COLORS.grid }}
                    tickLine={{ stroke: COLORS.grid }}
                  />
                  <Tooltip contentStyle={TOOLTIP_STYLE} />
                  <Line
                    type="monotone"
                    dataKey="cost_score"
                    stroke={COLORS.cost}
                    strokeWidth={2}
                    dot={{ r: 4, fill: COLORS.cost }}
                    activeDot={{ r: 6 }}
                    name="Cost Score"
                  />
                </LineChart>
              </ResponsiveContainer>
            </ChartCard>

            {/* Charts 3 + 4: side by side */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              {/* Chart 3: Agent-Model Assignment Mix (half width) */}
              <ChartCard
                title="Agent-Model Assignment Mix"
                subtitle="Counts model assignments recorded per agent in retrospectives."
                action={timeRangeAction}
              >
                <ResponsiveContainer width="100%" height={PIE_CHART_HEIGHT}>
                  <PieChart style={{ background: "transparent" }}>
                    <Pie
                      data={modelData}
                      dataKey="value"
                      nameKey="name"
                      cx="50%"
                      cy="44%"
                      innerRadius="45%"
                      outerRadius="70%"
                      paddingAngle={2}
                      label={false}
                    >
                      {modelData.map((entry, index) => (
                        <Cell key={`model-${index}`} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip contentStyle={TOOLTIP_STYLE} />
                    <Legend
                      wrapperStyle={{
                        fontFamily: "JetBrains Mono",
                        fontSize: 11,
                        color: "#999",
                      }}
                    />
                  </PieChart>
                </ResponsiveContainer>
              </ChartCard>

              {/* Chart 4: Executor Repair Frequency (half width) */}
              <ChartCard title="Executor Repair Frequency" action={timeRangeAction}>
                <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
                  <BarChart data={repairData} style={{ background: "transparent" }} margin={CHART_MARGIN}>
                    <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="executor"
                      tick={AXIS_TICK_STYLE}
                      axisLine={{ stroke: COLORS.grid }}
                      tickLine={{ stroke: COLORS.grid }}
                      interval={0}
                      angle={-35}
                      textAnchor="end"
                      height={72}
                      tickMargin={8}
                      tickFormatter={(value: string) => shortenLabel(value, 18)}
                    />
                    <YAxis
                      tick={AXIS_TICK_STYLE}
                      axisLine={{ stroke: COLORS.grid }}
                      tickLine={{ stroke: COLORS.grid }}
                      allowDecimals={false}
                    />
                    <Tooltip contentStyle={TOOLTIP_STYLE} />
                    <Bar
                      dataKey="repairs"
                      fill={COLORS.teal}
                      radius={[4, 4, 0, 0]}
                      name="Repairs"
                    />
                  </BarChart>
                </ResponsiveContainer>
              </ChartCard>
            </div>

            {/* Chart 5: Spawn Efficiency (full width) */}
            <ChartCard title="Spawn Efficiency" action={timeRangeAction}>
              <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
                <LineChart data={spawnData} style={{ background: "transparent" }} margin={CHART_MARGIN}>
                  <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
                  <XAxis
                    dataKey="task_id"
                    {...TASK_X_AXIS_PROPS}
                  />
                  <YAxis
                    tick={AXIS_TICK_STYLE}
                    axisLine={{ stroke: COLORS.grid }}
                    tickLine={{ stroke: COLORS.grid }}
                    allowDecimals={false}
                  />
                  <Tooltip contentStyle={TOOLTIP_STYLE} />
                  <Legend
                    wrapperStyle={{
                      fontFamily: "JetBrains Mono",
                      fontSize: 11,
                      color: "#999",
                    }}
                  />
                  <Line
                    type="monotone"
                    dataKey="total_spawns"
                    stroke={COLORS.quality}
                    strokeWidth={2}
                    dot={{ r: 4, fill: COLORS.quality }}
                    activeDot={{ r: 6 }}
                    name="Total Spawns"
                  />
                  <Line
                    type="monotone"
                    dataKey="wasted_spawns"
                    stroke={COLORS.red}
                    strokeWidth={2}
                    dot={{ r: 4, fill: COLORS.red }}
                    activeDot={{ r: 6 }}
                    name="Wasted Spawns"
                  />
                </LineChart>
              </ResponsiveContainer>
            </ChartCard>

            {/* ---- AC-22: 4 New Charts ---- */}

            {/* Chart 6: Token Cost Breakdown (stacked bar, full width) */}
            <ChartCard title="Token Cost Breakdown" action={timeRangeAction}>
              {tokenCostResult.agentKeys.length === 0 ? (
                <p className="text-slate-600 font-mono text-xs py-8 text-center">
                  {NA_LABEL} — No token usage data recorded yet.
                </p>
              ) : (
                <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
                  <BarChart data={tokenCostResult.data} style={{ background: "transparent" }} margin={CHART_MARGIN}>
                    <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="task_id"
                      {...TASK_X_AXIS_PROPS}
                    />
                    <YAxis
                      tick={AXIS_TICK_STYLE}
                      axisLine={{ stroke: COLORS.grid }}
                      tickLine={{ stroke: COLORS.grid }}
                      allowDecimals={false}
                      tickFormatter={(value: number) => formatTokens(value)}
                    />
                    <Tooltip
                      contentStyle={TOOLTIP_STYLE}
                      formatter={(value: number) => [formatTokens(value), undefined]}
                    />
                    <Legend
                      wrapperStyle={{
                        fontFamily: "JetBrains Mono",
                        fontSize: 11,
                        color: "#999",
                      }}
                    />
                    {tokenCostResult.agentKeys.map((agent, idx) => (
                      <Bar
                        key={agent}
                        dataKey={agent}
                        stackId="tokens"
                        fill={AGENT_ROLE_COLORS[idx % AGENT_ROLE_COLORS.length]}
                        name={agent}
                        radius={idx === tokenCostResult.agentKeys.length - 1 ? [4, 4, 0, 0] : undefined}
                      />
                    ))}
                  </BarChart>
                </ResponsiveContainer>
              )}
            </ChartCard>

            {/* Chart 6b: Token I/O — Input (uploaded) vs Output (downloaded) per task */}
            <ChartCard title="Token I/O per Task" action={timeRangeAction}>
              {tokenIOData.every((d) => d.input_tokens === 0 && d.output_tokens === 0) ? (
                <p className="text-slate-600 font-mono text-xs py-8 text-center">
                  {NA_LABEL} — No input/output token data recorded yet.
                </p>
              ) : (
                <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
                  <BarChart data={tokenIOData} style={{ background: "transparent" }} margin={CHART_MARGIN}>
                    <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
                    <XAxis
                      dataKey="task_id"
                      {...TASK_X_AXIS_PROPS}
                    />
                    <YAxis
                      tick={AXIS_TICK_STYLE}
                      axisLine={{ stroke: COLORS.grid }}
                      tickLine={{ stroke: COLORS.grid }}
                      allowDecimals={false}
                      tickFormatter={(v: number) => formatTokens(v)}
                    />
                    <Tooltip
                      contentStyle={TOOLTIP_STYLE}
                      formatter={(value: number) => [formatTokens(value), undefined]}
                    />
                    <Legend
                      wrapperStyle={{
                        fontFamily: "JetBrains Mono",
                        fontSize: 11,
                        color: "#999",
                      }}
                    />
                    <Bar
                      dataKey="input_tokens"
                      stackId="io"
                      fill="#B47AFF"
                      name="Input (uploaded)"
                      radius={[0, 0, 0, 0]}
                    />
                    <Bar
                      dataKey="output_tokens"
                      stackId="io"
                      fill="#BDF000"
                      name="Output (downloaded)"
                      radius={[4, 4, 0, 0]}
                    />
                  </BarChart>
                </ResponsiveContainer>
              )}
            </ChartCard>

            {/* Chart 7: Findings Per Task (bar, full width) */}
            <ChartCard title="Findings Per Task" action={timeRangeAction}>
              <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
                <BarChart
                  data={findingsData.map((d) => ({
                    ...d,
                    findings: typeof d.findings === "number" ? d.findings : 0,
                    hasData: typeof d.findings === "number",
                  }))}
                  style={{ background: "transparent" }}
                  margin={CHART_MARGIN}
                >
                  <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
                  <XAxis
                    dataKey="task_id"
                    {...TASK_X_AXIS_PROPS}
                  />
                  <YAxis
                    tick={AXIS_TICK_STYLE}
                    axisLine={{ stroke: COLORS.grid }}
                    tickLine={{ stroke: COLORS.grid }}
                    allowDecimals={false}
                  />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    formatter={((value: number, _name: string, props: { payload?: { hasData?: boolean } }) => {
                      if (props.payload && !props.payload.hasData) return [NA_LABEL, "Findings"];
                      return [value, "Findings"];
                    }) as never}
                  />
                  <Bar
                    dataKey="findings"
                    fill={COLORS.amber}
                    radius={[4, 4, 0, 0]}
                    name="Findings"
                  />
                </BarChart>
              </ResponsiveContainer>
            </ChartCard>

            {/* Chart 8: Repair Intensity vs Findings (line, full width) */}
            <ChartCard
              title="Repair Intensity vs Findings"
              subtitle="Proxy metric: repair cycles divided by findings, capped at 100%."
              action={timeRangeAction}
            >
              <ResponsiveContainer width="100%" height={CHART_HEIGHT}>
                <LineChart data={repairRateData} style={{ background: "transparent" }} margin={CHART_MARGIN}>
                  <CartesianGrid stroke={COLORS.grid} strokeDasharray="3 3" />
                  <XAxis
                    dataKey="task_id"
                    {...TASK_X_AXIS_PROPS}
                  />
                  <YAxis
                    domain={[0, 100]}
                    tick={AXIS_TICK_STYLE}
                    axisLine={{ stroke: COLORS.grid }}
                    tickLine={{ stroke: COLORS.grid }}
                    unit="%"
                  />
                  <Tooltip
                    contentStyle={TOOLTIP_STYLE}
                    formatter={((value: number | null) => {
                      if (value === null || value === undefined) return [NA_LABEL, "Success Rate"];
                      return [`${value}%`, "Success Rate"];
                    }) as never}
                  />
                  <Line
                    type="monotone"
                    dataKey="success_rate"
                    stroke={COLORS.teal}
                    strokeWidth={2}
                    dot={{ r: 4, fill: COLORS.teal }}
                    activeDot={{ r: 6 }}
                    name="Success Rate"
                    connectNulls={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </ChartCard>

            {/* Chart 9: Routing Distribution (pie, half width) */}
            <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
              <ChartCard title="Routing Distribution" action={timeRangeAction}>
                {routingData.length === 0 ? (
                  <p className="text-slate-600 font-mono text-xs py-8 text-center">
                    {NA_LABEL} — No routing data available.
                  </p>
                ) : (
                  <ResponsiveContainer width="100%" height={PIE_CHART_HEIGHT}>
                    <PieChart style={{ background: "transparent" }}>
                      <Pie
                        data={routingData}
                        dataKey="value"
                        nameKey="name"
                        cx="50%"
                        cy="44%"
                        innerRadius="45%"
                        outerRadius="70%"
                        paddingAngle={2}
                        label={false}
                      >
                        {routingData.map((entry, index) => (
                          <Cell key={`routing-${index}`} fill={entry.color} />
                        ))}
                      </Pie>
                      <Tooltip contentStyle={TOOLTIP_STYLE} />
                      <Legend
                        wrapperStyle={{
                          fontFamily: "JetBrains Mono",
                          fontSize: 11,
                          color: "#999",
                        }}
                      />
                    </PieChart>
                  </ResponsiveContainer>
                )}
              </ChartCard>
            </div>
          </div>
          )}
        </TabsContent>

        <TabsContent value="costs">
          {!data || data.length === 0 ? (
            <InsufficientDataState count={data?.length ?? 0} />
          ) : (
            <CostsTabContent retros={sorted} />
          )}
        </TabsContent>

        <TabsContent value="repo">
          {reportLoading && !repoReport && statsLoading && !projectStats && stateLoading && !repoState ? (
            <LoadingState />
          ) : reportError && !repoReport && statsError && !projectStats ? (
            <ErrorState
              message={[reportError, statsError, stateError].filter(Boolean).join(" | ")}
              onRetry={() => {
                refetchReport();
                refetchStats();
                refetchState();
              }}
            />
          ) : (
            <RepoTabContent
              report={repoReport}
              state={repoState}
              stateError={stateError}
              projectStats={projectStats}
              totalTaskCount={tasks?.length ?? projectStats?.total_tasks ?? 0}
            />
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
