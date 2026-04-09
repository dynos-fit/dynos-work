/**
 * Learning Ops page — /learning-ops
 *
 * Unified view of the learning system health: daemon status, maintenance
 * cycles, promotion funnel, benchmark freshness, coverage gaps, and
 * attention queue. Fetches from three endpoints on mount.
 */
import { useMemo } from "react";
import { motion } from "motion/react";
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip as RechartsTooltip,
  Cell,
  Pie,
  PieChart,
  Legend,
} from "recharts";
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Cpu,
  RefreshCw,
  Shield,
  XCircle,
} from "lucide-react";
import { usePollingData } from "@/data/hooks";
import type {
  MaintainerStatus,
  MaintenanceCycle,
  ControlPlaneData,
  LearnedAgent,
  RepoCoverageGap,
  AttentionItem,
  FreshnessBucket,
} from "@/data/types";
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricCard } from "@/components/MetricCard";
import { ChartCard } from "@/components/ChartCard";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const CARD_BASE =
  "border border-white/5 bg-[#0F1114]/60 backdrop-blur-md p-6 rounded-xl";

const COLORS = {
  primary: "#BDF000",
  secondary: "#2DD4A8",
  warning: "#FF6D00",
  danger: "#FF3B3B",
  purple: "#B47AFF",
} as const;

const MODE_COLORS: Record<string, string> = {
  replace: COLORS.primary,
  alongside: COLORS.secondary,
  shadow: COLORS.purple,
};

const FRESHNESS_COLORS: Record<string, string> = {
  Fresh: COLORS.primary,
  Recent: COLORS.secondary,
  Aging: COLORS.warning,
  Stale: COLORS.danger,
  Unbenchmarked: "#7A776E",
};

const CHART_HEIGHT = 280;
const PIE_HEIGHT = 320;
const CHART_MARGIN = { top: 8, right: 16, left: 0, bottom: 8 } as const;
const AXIS_TICK_STYLE = {
  fill: "#999",
  fontFamily: "JetBrains Mono",
  fontSize: 11,
} as const;
const TOOLTIP_STYLE = {
  backgroundColor: "#1A1F2E",
  border: "1px solid #333",
  borderRadius: 8,
  fontFamily: "JetBrains Mono",
  fontSize: 11,
  color: "#ccc",
} as const;

const URGENCY_ORDER: Record<string, number> = {
  "demoted on regression": 0,
  unbenchmarked: 1,
  "stale benchmark": 2,
  "coverage gap": 3,
};

const POLL_INTERVAL = 30_000;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getBannerColor(opts: {
  running: boolean;
  lastCycleOk: boolean;
  queueBacklog: number;
}): "green" | "amber" | "red" {
  if (!opts.running) return "red";
  if (!opts.lastCycleOk || opts.queueBacklog > 0) return "amber";
  return "green";
}

const BANNER_COLOR_MAP: Record<string, { border: string; bg: string; text: string }> = {
  green: { border: "border-green-500/30", bg: "bg-green-500/10", text: "text-green-400" },
  amber: { border: "border-yellow-500/30", bg: "bg-yellow-500/10", text: "text-yellow-400" },
  red: { border: "border-red-500/30", bg: "bg-red-500/10", text: "text-red-400" },
};

function formatRelativeTime(iso: string): string {
  if (!iso) return "n/a";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function formatTimestamp(iso: string): string {
  if (!iso) return "n/a";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatDate(iso: string | undefined): string {
  if (!iso) return "n/a";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function classifyFreshness(offset: number, hasBenchmark: boolean): string {
  if (!hasBenchmark) return "Unbenchmarked";
  if (offset === 0) return "Fresh";
  if (offset <= 2) return "Recent";
  if (offset <= 5) return "Aging";
  return "Stale";
}

// ---------------------------------------------------------------------------
// Sub-components: Skeletons
// ---------------------------------------------------------------------------

function BannerSkeleton() {
  return (
    <div className={CARD_BASE} role="status" aria-label="Loading health banner">
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3">
        {Array.from({ length: 7 }).map((_, i) => (
          <div key={i}>
            <Skeleton className="h-3 w-16 mb-2" />
            <Skeleton className="h-6 w-12" />
          </div>
        ))}
      </div>
    </div>
  );
}

function SectionSkeleton({ label }: { label: string }) {
  return (
    <div className={CARD_BASE} role="status" aria-label={`Loading ${label}`}>
      <Skeleton className="h-5 w-48 mb-4" />
      <Skeleton className="h-52 w-full" />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-components: Error
// ---------------------------------------------------------------------------

function SectionError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      className="border border-red-500/30 bg-red-500/10 backdrop-blur-md p-6 text-center rounded-xl"
      role="alert"
    >
      <XCircle className="w-6 h-6 text-red-400 mx-auto mb-2" aria-hidden="true" />
      <p className="text-slate-400 text-sm font-mono mb-4">{message}</p>
      <button
        onClick={onRetry}
        className="px-4 py-2 bg-[#BDF000]/5 hover:bg-[#BDF000]/20 text-[#BDF000] border border-[#BDF000]/20 font-mono text-xs transition-colors rounded-xl"
        aria-label="Retry loading"
      >
        RETRY
      </button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Sub-component: Health Banner (AC-4)
// ---------------------------------------------------------------------------

interface BannerProps {
  maintainer: MaintainerStatus;
  controlPlane: ControlPlaneData;
}

function HealthBanner({ maintainer, controlPlane }: BannerProps) {
  const lastCycleOk = maintainer.last_cycle?.ok ?? true;
  const queueBacklog = controlPlane.queue?.items?.length ?? 0;
  const bannerColor = getBannerColor({
    running: maintainer.running,
    lastCycleOk,
    queueBacklog,
  });
  const colorClasses = BANNER_COLOR_MAP[bannerColor];
  const summary = controlPlane.agent_summary ?? { total: 0, routeable: 0, shadow: 0, demoted: 0 };

  const fields: Array<{ label: string; value: string | number }> = [
    { label: "Daemon", value: maintainer.running ? "Running" : "Stopped" },
    { label: "PID", value: maintainer.pid ?? "n/a" },
    { label: "Last Cycle", value: maintainer.last_cycle?.executed_at ? formatRelativeTime(maintainer.last_cycle.executed_at) : "n/a" },
    { label: "Cycle Status", value: lastCycleOk ? "OK" : "Failed" },
    { label: "Total Cycles", value: maintainer.cycle_count ?? 0 },
    { label: "Poll Interval", value: `${maintainer.poll_seconds ?? 0}s` },
    { label: "Total Agents", value: summary.total },
    { label: "Routeable", value: summary.routeable },
    { label: "Shadow", value: summary.shadow },
    { label: "Demoted", value: summary.demoted },
    { label: "Queue", value: queueBacklog },
    { label: "Coverage Gaps", value: controlPlane.coverage_gaps?.length ?? 0 },
  ];

  return (
    <motion.div
      className={`${CARD_BASE} ${colorClasses.border} ${colorClasses.bg}`}
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3 }}
      role="banner"
      aria-label="Learning system health"
    >
      <div className="flex items-center gap-2 mb-4">
        {bannerColor === "green" && <CheckCircle2 className="w-4 h-4 text-green-400" aria-hidden="true" />}
        {bannerColor === "amber" && <AlertTriangle className="w-4 h-4 text-yellow-400" aria-hidden="true" />}
        {bannerColor === "red" && <XCircle className="w-4 h-4 text-red-400" aria-hidden="true" />}
        <span className={`text-xs font-mono uppercase tracking-wider ${colorClasses.text}`}>
          System Health
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-x-6 gap-y-3">
        {fields.map((field) => (
          <div key={field.label}>
            <div className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">
              {field.label}
            </div>
            <div className="text-sm font-mono text-slate-200 mt-0.5 truncate" title={String(field.value)}>
              {field.value}
            </div>
          </div>
        ))}
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Section 1: Maintenance Cycles (AC-5)
// ---------------------------------------------------------------------------

interface CyclesSectionProps {
  cycles: MaintenanceCycle[];
  totalCycles: number;
}

function MaintenanceCyclesSection({ cycles, totalCycles }: CyclesSectionProps) {
  const last20 = cycles.slice(-20);
  const totalFailures = cycles.filter((c) => !(c.ok ?? c.failed_steps.length === 0)).length;
  const failureRate = totalCycles === 0 ? 0 : (totalFailures / totalCycles) * 100;

  return (
    <motion.div
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: 0.1, duration: 0.3 }}
    >
      <ChartCard title="Maintenance Cycles" subtitle="Daemon cycle history and failure tracking.">
        <div className="grid grid-cols-3 gap-4 mb-6">
          <div>
            <div className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">Total Cycles</div>
            <div className="text-xl font-mono text-slate-200">{totalCycles}</div>
          </div>
          <div>
            <div className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">Total Failures</div>
            <div className="text-xl font-mono text-red-400">{totalFailures}</div>
          </div>
          <div>
            <div className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">Failure Rate</div>
            <div className="text-xl font-mono text-slate-200">{failureRate.toFixed(1)}%</div>
          </div>
        </div>

        {/* Cycle outcome dots */}
        <div className="mb-4">
          <div className="flex items-center gap-1.5 flex-wrap" role="img" aria-label="Cycle outcomes timeline">
            {last20.map((cycle, idx) => {
              const ok = cycle.ok ?? cycle.failed_steps.length === 0;
              return (
                <div
                  key={`${cycle.executed_at}-${idx}`}
                  className="w-3 h-3 rounded-full flex-shrink-0"
                  style={{ backgroundColor: ok ? COLORS.secondary : COLORS.danger }}
                  title={`${formatTimestamp(cycle.executed_at)}: ${ok ? "OK" : "Failed"}`}
                  aria-hidden="true"
                />
              );
            })}
          </div>
          <p className="text-[10px] text-slate-500 font-mono mt-2">
            Cycle outcomes (last 20).
          </p>
        </div>

        {cycles.length === 0 ? (
          <p className="text-slate-600 font-mono text-xs py-8 text-center">
            No maintenance cycles recorded yet.
          </p>
        ) : (
          <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
            <Table>
              <TableHeader>
                <TableRow className="border-b border-white/10">
                  <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Timestamp</TableHead>
                  <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Status</TableHead>
                  <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Failed Steps</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {last20.map((cycle, idx) => {
                  const ok = cycle.ok ?? cycle.failed_steps.length === 0;
                  return (
                    <TableRow key={`${cycle.executed_at}-${idx}`} className="border-b border-white/5">
                      <TableCell className="text-xs font-mono text-slate-400">{formatTimestamp(cycle.executed_at)}</TableCell>
                      <TableCell>
                        <Badge
                          variant="outline"
                          className={`font-mono text-[10px] uppercase ${ok ? "text-green-400 border-green-500/30" : "text-red-400 border-red-500/30"}`}
                        >
                          {ok ? "OK" : "Failed"}
                        </Badge>
                      </TableCell>
                      <TableCell className="text-xs font-mono text-slate-400">
                        {cycle.failed_steps.length === 0 ? "none" : cycle.failed_steps.join(", ")}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </ChartCard>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Section 2: Promotion Funnel (AC-6)
// ---------------------------------------------------------------------------

interface FunnelSectionProps {
  agents: LearnedAgent[];
}

function PromotionFunnelSection({ agents }: FunnelSectionProps) {
  const funnelData = useMemo(() => {
    const shadow = agents.filter((a) => a.mode === "shadow").length;
    const alongside = agents.filter((a) => a.mode === "alongside").length;
    const replace = agents.filter((a) => a.mode === "replace").length;
    return [{ name: "Distribution", shadow, alongside, replace }];
  }, [agents]);

  const demotedCount = useMemo(() => agents.filter((a) => a.mode === "demoted" || a.status.includes("demoted")).length, [agents]);

  if (agents.length === 0) {
    return (
      <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15, duration: 0.3 }}>
        <ChartCard title="Promotion Funnel" subtitle="Agent mode distribution and roster.">
          <p className="text-slate-600 font-mono text-xs py-8 text-center">
            No learned agents registered.
          </p>
        </ChartCard>
      </motion.div>
    );
  }

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.15, duration: 0.3 }}>
      <ChartCard title="Promotion Funnel" subtitle="Agent mode distribution and roster.">
        <div className="mb-4">
          <ResponsiveContainer width="100%" height={80}>
            <BarChart data={funnelData} layout="vertical" margin={{ top: 0, right: 16, left: 0, bottom: 0 }}>
              <XAxis type="number" hide />
              <YAxis type="category" dataKey="name" hide />
              <RechartsTooltip contentStyle={TOOLTIP_STYLE} />
              <Bar dataKey="shadow" stackId="a" fill={COLORS.purple} name="Shadow" radius={[4, 0, 0, 4]} />
              <Bar dataKey="alongside" stackId="a" fill={COLORS.secondary} name="Alongside" />
              <Bar dataKey="replace" stackId="a" fill={COLORS.primary} name="Replace" radius={[0, 4, 4, 0]} />
            </BarChart>
          </ResponsiveContainer>
          <div className="flex items-center gap-4 mt-2">
            <span className="text-[10px] font-mono text-slate-500 flex items-center gap-1">
              <span className="w-2 h-2 rounded-full inline-block" style={{ backgroundColor: COLORS.purple }} aria-hidden="true" /> Shadow
            </span>
            <span className="text-[10px] font-mono text-slate-500 flex items-center gap-1">
              <span className="w-2 h-2 rounded-full inline-block" style={{ backgroundColor: COLORS.secondary }} aria-hidden="true" /> Alongside
            </span>
            <span className="text-[10px] font-mono text-slate-500 flex items-center gap-1">
              <span className="w-2 h-2 rounded-full inline-block" style={{ backgroundColor: COLORS.primary }} aria-hidden="true" /> Replace
            </span>
            <span className="text-[10px] font-mono text-slate-500 ml-auto">
              Demoted: <span className="text-red-400">{demotedCount}</span>
            </span>
          </div>
          <p className="text-[10px] text-slate-500 font-mono mt-2">
            Current-state distribution (not historical transitions).
          </p>
        </div>

        {/* Agent table */}
        <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-b border-white/10">
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Name</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Kind</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Role</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Task Type</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Mode</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Status</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Route</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Composite</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Evaluated</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Generated From</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {agents.map((agent) => (
                <TableRow key={`${agent.agent_name}-${agent.role}-${agent.task_type}`} className="border-b border-white/5">
                  <TableCell className="text-xs font-mono text-slate-300 max-w-[160px] truncate" title={agent.agent_name}>
                    {agent.agent_name}
                  </TableCell>
                  <TableCell className="text-xs font-mono text-slate-400">{agent.item_kind}</TableCell>
                  <TableCell className="text-xs font-mono text-slate-400">{agent.role}</TableCell>
                  <TableCell className="text-xs font-mono text-slate-400">{agent.task_type}</TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className="font-mono text-[10px] uppercase"
                      style={{ borderColor: `${MODE_COLORS[agent.mode] ?? "#7A776E"}55`, color: MODE_COLORS[agent.mode] ?? "#7A776E" }}
                    >
                      {agent.mode}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs font-mono text-slate-400">{agent.status}</TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={`font-mono text-[10px] uppercase ${agent.route_allowed ? "text-green-400 border-green-500/30" : "text-red-400 border-red-500/30"}`}
                    >
                      {agent.route_allowed ? "Yes" : "No"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs font-mono text-slate-300">
                    {agent.benchmark_summary?.mean_composite?.toFixed(2) ?? "n/a"}
                  </TableCell>
                  <TableCell className="text-xs font-mono text-slate-400">
                    {formatDate(agent.last_evaluation?.evaluated_at)}
                  </TableCell>
                  <TableCell className="text-xs font-mono text-slate-400 max-w-[120px] truncate" title={agent.generated_from}>
                    {agent.generated_from || "n/a"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </ChartCard>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Section 3: Benchmark Freshness & Coverage (AC-7)
// ---------------------------------------------------------------------------

interface FreshnessSectionProps {
  agents: LearnedAgent[];
  freshnessBuckets: FreshnessBucket[];
  coverageGaps: RepoCoverageGap[];
  recentRuns: Array<Record<string, unknown>>;
}

function BenchmarkFreshnessSection({ agents, freshnessBuckets, coverageGaps, recentRuns }: FreshnessSectionProps) {
  const pieData = useMemo(() => {
    // Prefer server-computed buckets, fall back to client-computed
    if (freshnessBuckets && freshnessBuckets.length > 0) {
      return freshnessBuckets.map((b) => ({
        name: b.label,
        value: b.count,
        color: FRESHNESS_COLORS[b.label] ?? "#7A776E",
      }));
    }
    const counts: Record<string, number> = { Fresh: 0, Recent: 0, Aging: 0, Stale: 0, Unbenchmarked: 0 };
    for (const agent of agents) {
      const hasBenchmark = agent.benchmark_summary != null && (agent.benchmark_summary.sample_count ?? 0) > 0;
      const bucket = classifyFreshness(agent.last_benchmarked_task_offset, hasBenchmark);
      counts[bucket]++;
    }
    return Object.entries(counts)
      .filter(([, count]) => count > 0)
      .map(([name, value]) => ({ name, value, color: FRESHNESS_COLORS[name] ?? "#7A776E" }));
  }, [agents, freshnessBuckets]);

  const last10Runs = recentRuns.slice(-10);

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.2, duration: 0.3 }}>
      <div className="space-y-6">
        <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
          {/* Freshness Pie Chart */}
          <ChartCard title="Benchmark Freshness" subtitle="Distribution of benchmark staleness across agents.">
            {pieData.length === 0 ? (
              <p className="text-slate-600 font-mono text-xs py-8 text-center">No agent data available.</p>
            ) : (
              <ResponsiveContainer width="100%" height={PIE_HEIGHT}>
                <PieChart>
                  <Pie
                    data={pieData}
                    dataKey="value"
                    nameKey="name"
                    cx="50%"
                    cy="44%"
                    innerRadius="45%"
                    outerRadius="70%"
                    paddingAngle={2}
                    label={false}
                  >
                    {pieData.map((entry) => (
                      <Cell key={entry.name} fill={entry.color} />
                    ))}
                  </Pie>
                  <RechartsTooltip contentStyle={TOOLTIP_STYLE} formatter={(value: number) => [value, "Agents"]} />
                  <Legend wrapperStyle={{ fontFamily: "JetBrains Mono", fontSize: 11, color: "#999" }} />
                </PieChart>
              </ResponsiveContainer>
            )}
            <p className="text-[10px] text-slate-500 font-mono mt-2">
              Based on last_benchmarked_task_offset at time of page load.
            </p>
          </ChartCard>

          {/* Coverage Gap Table */}
          <ChartCard title="Coverage Gaps" subtitle="Roles and task types with no learned agent.">
            {coverageGaps.length === 0 ? (
              <p className="text-slate-600 font-mono text-xs py-8 text-center">No coverage gaps detected.</p>
            ) : (
              <div className="overflow-x-auto max-h-[320px] overflow-y-auto">
                <Table>
                  <TableHeader>
                    <TableRow className="border-b border-white/10">
                      <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Target</TableHead>
                      <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Role</TableHead>
                      <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Task Type</TableHead>
                      <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Kind</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {coverageGaps.map((gap, idx) => (
                      <TableRow key={`${gap.target_name}-${idx}`} className="border-b border-white/5">
                        <TableCell className="text-xs font-mono text-slate-300 max-w-[160px] truncate" title={gap.target_name}>
                          {gap.target_name}
                        </TableCell>
                        <TableCell className="text-xs font-mono text-slate-400">{gap.role}</TableCell>
                        <TableCell className="text-xs font-mono text-slate-400">{gap.task_type}</TableCell>
                        <TableCell className="text-xs font-mono text-slate-400">{gap.item_kind}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
          </ChartCard>
        </div>

        {/* Recent Benchmark Runs */}
        <ChartCard title="Recent Benchmark Runs" subtitle="Last 10 benchmark runs across all agents.">
          {last10Runs.length === 0 ? (
            <p className="text-slate-600 font-mono text-xs py-8 text-center">No benchmark runs recorded yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <Table>
                <TableHeader>
                  <TableRow className="border-b border-white/10">
                    <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Run ID</TableHead>
                    <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Target</TableHead>
                    <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Role</TableHead>
                    <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Task Type</TableHead>
                    <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Executed</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {last10Runs.map((run, idx) => (
                    <TableRow key={`${String(run.run_id ?? idx)}-${idx}`} className="border-b border-white/5">
                      <TableCell className="text-xs font-mono text-slate-300 max-w-[120px] truncate" title={String(run.run_id ?? "")}>
                        {String(run.run_id ?? "n/a")}
                      </TableCell>
                      <TableCell className="text-xs font-mono text-slate-400">{String(run.target_name ?? "n/a")}</TableCell>
                      <TableCell className="text-xs font-mono text-slate-400">{String(run.role ?? "n/a")}</TableCell>
                      <TableCell className="text-xs font-mono text-slate-400">{String(run.task_type ?? "n/a")}</TableCell>
                      <TableCell className="text-xs font-mono text-slate-400">{formatTimestamp(String(run.executed_at ?? ""))}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </ChartCard>
      </div>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Section 4: Attention Queue (AC-8)
// ---------------------------------------------------------------------------

interface AttentionSectionProps {
  items: AttentionItem[];
}

function AttentionQueueSection({ items }: AttentionSectionProps) {
  const sorted = useMemo(
    () =>
      [...items].sort(
        (a, b) => (URGENCY_ORDER[a.reason] ?? 99) - (URGENCY_ORDER[b.reason] ?? 99),
      ),
    [items],
  );

  if (sorted.length === 0) {
    return (
      <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25, duration: 0.3 }}>
        <ChartCard title="Attention Queue" subtitle="Items requiring manual review or intervention.">
          <div className="py-8 text-center">
            <CheckCircle2 className="w-8 h-8 text-green-400 mx-auto mb-2" aria-hidden="true" />
            <p className="text-slate-400 font-mono text-sm">Nothing needs attention</p>
          </div>
        </ChartCard>
      </motion.div>
    );
  }

  const reasonBadgeColor: Record<string, string> = {
    "demoted on regression": "text-red-400 border-red-500/30",
    unbenchmarked: "text-yellow-400 border-yellow-500/30",
    "stale benchmark": "text-orange-400 border-orange-500/30",
    "coverage gap": "text-slate-400 border-slate-500/30",
  };

  return (
    <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25, duration: 0.3 }}>
      <ChartCard title="Attention Queue" subtitle="Items requiring manual review or intervention.">
        <div className="overflow-x-auto max-h-[400px] overflow-y-auto">
          <Table>
            <TableHeader>
              <TableRow className="border-b border-white/10">
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Agent</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Reason</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Mode</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Status</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Recommendation</TableHead>
                <TableHead className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">Delta</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map((item, idx) => (
                <TableRow key={`${item.agent_name}-${item.reason}-${idx}`} className="border-b border-white/5">
                  <TableCell className="text-xs font-mono text-slate-300 max-w-[160px] truncate" title={item.agent_name}>
                    {item.agent_name}
                  </TableCell>
                  <TableCell>
                    <Badge
                      variant="outline"
                      className={`font-mono text-[10px] ${reasonBadgeColor[item.reason] ?? "text-slate-400 border-slate-500/30"}`}
                    >
                      {item.reason}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs font-mono text-slate-400">{item.mode}</TableCell>
                  <TableCell className="text-xs font-mono text-slate-400">{item.status}</TableCell>
                  <TableCell className="text-xs font-mono text-slate-400">{item.recommendation ?? "n/a"}</TableCell>
                  <TableCell className="text-xs font-mono text-slate-300">
                    {item.delta_composite != null ? item.delta_composite.toFixed(2) : "n/a"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </ChartCard>
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Main Page Component
// ---------------------------------------------------------------------------

interface CyclesResponse {
  total_cycles: number;
  cycles: MaintenanceCycle[];
}

export default function LearningOps() {
  const status = usePollingData<MaintainerStatus>("/api/maintainer-status", POLL_INTERVAL);
  const cyclesData = usePollingData<CyclesResponse>("/api/maintenance-cycles", POLL_INTERVAL);
  const controlPlane = usePollingData<ControlPlaneData>("/api/control-plane", POLL_INTERVAL);

  const statusLoading = status.loading && status.data === null;
  const cyclesLoading = cyclesData.loading && cyclesData.data === null;
  const cpLoading = controlPlane.loading && controlPlane.data === null;

  const statusError = status.error !== null && status.data === null;
  const cyclesError = cyclesData.error !== null && cyclesData.data === null;
  const cpError = controlPlane.error !== null && controlPlane.data === null;

  return (
    <div className="p-8 h-full flex flex-col">
      <header className="mb-8">
        <h1 className="text-3xl font-mono font-light tracking-[0.2em] text-[#BDF000]">LEARNING OPS</h1>
        <p className="text-slate-500 font-mono text-xs mt-2">
          // DAEMON HEALTH, MAINTENANCE CYCLES, PROMOTION FUNNEL, BENCHMARK COVERAGE
        </p>
      </header>

      <div className="space-y-6 flex-1 overflow-auto">
        {/* Health Banner */}
        {(statusLoading || cpLoading) && !status.data && !controlPlane.data && <BannerSkeleton />}
        {statusError && !cpError && (
          <SectionError message={`Failed to load maintainer status. ${status.error ?? ""}`} onRetry={status.refetch} />
        )}
        {cpError && !statusError && (
          <SectionError message={`Failed to load control plane. ${controlPlane.error ?? ""}`} onRetry={controlPlane.refetch} />
        )}
        {statusError && cpError && (
          <SectionError
            message={`Failed to load system data. ${status.error ?? ""}`}
            onRetry={() => { status.refetch(); controlPlane.refetch(); }}
          />
        )}
        {status.data && controlPlane.data && (
          <HealthBanner maintainer={status.data} controlPlane={controlPlane.data} />
        )}

        {/* Section 1: Maintenance Cycles */}
        {cyclesLoading && <SectionSkeleton label="maintenance cycles" />}
        {cyclesError && (
          <SectionError message={`Failed to load maintenance cycles. ${cyclesData.error ?? ""}`} onRetry={cyclesData.refetch} />
        )}
        {cyclesData.data && (
          <MaintenanceCyclesSection
            cycles={cyclesData.data.cycles}
            totalCycles={cyclesData.data.total_cycles}
          />
        )}

        {/* Section 2: Promotion Funnel */}
        {cpLoading && <SectionSkeleton label="promotion funnel" />}
        {cpError && !controlPlane.data && (
          <SectionError message={`Failed to load agent data. ${controlPlane.error ?? ""}`} onRetry={controlPlane.refetch} />
        )}
        {controlPlane.data && (
          <PromotionFunnelSection agents={controlPlane.data.agents ?? []} />
        )}

        {/* Section 3: Benchmark Freshness & Coverage */}
        {cpLoading && <SectionSkeleton label="benchmark freshness" />}
        {controlPlane.data && (
          <BenchmarkFreshnessSection
            agents={controlPlane.data.agents ?? []}
            freshnessBuckets={controlPlane.data.freshness_buckets ?? []}
            coverageGaps={controlPlane.data.coverage_gaps ?? []}
            recentRuns={controlPlane.data.recent_runs ?? []}
          />
        )}

        {/* Section 4: Attention Queue */}
        {cpLoading && <SectionSkeleton label="attention queue" />}
        {controlPlane.data && (
          <AttentionQueueSection items={controlPlane.data.attention_items ?? []} />
        )}
      </div>
    </div>
  );
}
