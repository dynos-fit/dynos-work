import { useMemo, useState, useEffect, useCallback } from "react";
import { motion } from "motion/react";
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  Clock,
  FileCode2,
  GitBranch,
  MonitorDot,
  RefreshCw,
  Shield,
  Sparkles,
  Terminal,
  TestTube2,
} from "lucide-react";
import { usePollingData } from "@/data/hooks";
import { useProject } from "@/data/ProjectContext";
import type {
  AutofixMetrics,
  RepoProjectStats,
  RepoReport,
  RepoState,
  TaskManifest,
  TaskRetrospective,
} from "@/data/types";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricCard } from "@/components/MetricCard";
import { ChartCard } from "@/components/ChartCard";

interface ExecutionLogResponse {
  lines: string[];
}

interface HealthResult {
  endpoint: string;
  ok: boolean;
  ms: number | null;
}

const HEALTH_ENDPOINTS = [
  "/api/tasks",
  "/api/agents",
  "/api/findings",
  "/api/autofix-metrics",
  "/api/report",
  "/api/project-stats",
] as const;

const HEALTH_PING_TIMEOUT_MS = 5000;
const HEALTH_POLL_INTERVAL_MS = 10000;

function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "n/a";
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return iso;
  }
}

function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  try {
    const diffMs = Date.now() - new Date(iso).getTime();
    if (diffMs < 60_000) return "just now";
    const minutes = Math.floor(diffMs / 60_000);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  } catch {
    return "unknown";
  }
}

function formatCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return `${value}`;
}

function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return "--";
  return `${Math.round(value * 100)}%`;
}

function truncate(value: string, max: number): string {
  return value.length > max ? `${value.slice(0, max - 3)}...` : value;
}

function isDaemonActive(lines: string[]): boolean {
  if (lines.length === 0) return false;
  const lastLine = lines[lines.length - 1];
  const match = lastLine.match(/^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)/);
  if (!match) return false;
  const timestamp = new Date(match[1]).getTime();
  return timestamp > Date.now() - 60 * 60 * 1000;
}

function TableBlock({
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
                  className={`py-2 pr-4 align-top ${cellIndex === row.length - 1 ? "text-slate-300" : "text-slate-400"}`}
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

function StatusPill({
  label,
  color,
}: {
  label: string;
  color: string;
}) {
  return (
    <span
      className="inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[10px] font-mono uppercase tracking-wider"
      style={{ borderColor: `${color}55`, color }}
    >
      <span className="w-2 h-2 rounded-full" style={{ backgroundColor: color }} aria-hidden="true" />
      {label}
    </span>
  );
}

function AttentionItem({
  title,
  value,
  detail,
  tone,
}: {
  title: string;
  value: string;
  detail: string;
  tone: "good" | "warn" | "bad" | "neutral";
}) {
  const colors = {
    good: "#2DD4A8",
    warn: "#FF9F43",
    bad: "#FF3B3B",
    neutral: "#B47AFF",
  } as const;
  const color = colors[tone];

  return (
    <div className="rounded-2xl border border-white/6 bg-black/20 p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[10px] font-mono text-slate-500 uppercase tracking-wider">{title}</span>
        <span className="text-lg font-mono" style={{ color }}>{value}</span>
      </div>
      <p className="text-xs font-mono text-slate-400 leading-relaxed">{detail}</p>
    </div>
  );
}

function FeedCard({
  lines,
}: {
  lines: string[];
}) {
  if (lines.length === 0) {
    return (
      <div className="flex flex-col items-start gap-2 py-4">
        <Terminal className="w-5 h-5 text-slate-600" aria-hidden="true" />
        <p className="text-xs text-slate-500 font-mono">No recent execution logs.</p>
        <p className="text-xs text-slate-600 font-mono">Start or complete a task to populate the live feed.</p>
      </div>
    );
  }

  return (
    <div className="space-y-2 font-mono text-sm">
      {lines.slice(-8).map((line, index) => (
        <motion.div
          key={`${line}-${index}`}
          initial={{ opacity: 0, x: -8 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.2 }}
          className="text-[#2DD4A8]/80 flex gap-2 items-start"
        >
          <span className="text-slate-600 shrink-0" aria-hidden="true">&gt;</span>
          <span className="break-all line-clamp-2">{line}</span>
        </motion.div>
      ))}
    </div>
  );
}

function HealthCheckCard() {
  const [results, setResults] = useState<HealthResult[]>([]);
  const [lastChecked, setLastChecked] = useState<Date | null>(null);
  const [isPinging, setIsPinging] = useState(false);

  const pingAll = useCallback(async () => {
    setIsPinging(true);
    const newResults: HealthResult[] = await Promise.all(
      HEALTH_ENDPOINTS.map(async (endpoint) => {
        const start = performance.now();
        try {
          const controller = new AbortController();
          const timeoutId = setTimeout(() => controller.abort(), HEALTH_PING_TIMEOUT_MS);
          const res = await fetch(endpoint);
          clearTimeout(timeoutId);
          return { endpoint, ok: res.ok, ms: Math.round(performance.now() - start) };
        } catch {
          const elapsed = performance.now() - start;
          return { endpoint, ok: false, ms: elapsed >= HEALTH_PING_TIMEOUT_MS ? null : Math.round(elapsed) };
        }
      }),
    );
    setResults(newResults);
    setLastChecked(new Date());
    setIsPinging(false);
  }, []);

  useEffect(() => {
    pingAll();
    const interval = setInterval(pingAll, HEALTH_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [pingAll]);

  return (
    <ChartCard
      title="API Health"
      subtitle="Dashboard endpoint reachability and latency snapshot."
      action={
        <button
          onClick={pingAll}
          disabled={isPinging}
          className="flex items-center gap-1.5 px-3 py-1 text-[10px] font-mono tracking-wider rounded-full border border-white/10 text-[#D4D0C8] hover:border-[#BDF000]/40 hover:text-[#BDF000] hover:bg-[#BDF000]/5 transition-colors disabled:opacity-40"
          aria-label="Refresh health check"
        >
          <RefreshCw className={`w-3 h-3 ${isPinging ? "animate-spin" : ""}`} aria-hidden="true" />
          Refresh
        </button>
      }
    >
      {results.length === 0 ? (
        <div className="space-y-3" role="status" aria-label="Loading health check data">
          {Array.from({ length: HEALTH_ENDPOINTS.length }).map((_, i) => (
            <Skeleton key={i} className="h-4 bg-white/5" style={{ width: `${60 + i * 4}%` }} />
          ))}
        </div>
      ) : (
        <div className="space-y-2 font-mono text-xs">
          {results.map((result) => (
            <div key={result.endpoint} className="flex items-center gap-3 py-1 border-b border-white/5 last:border-b-0">
              <span
                className="w-2.5 h-2.5 rounded-full shrink-0"
                style={{ backgroundColor: result.ok ? "#BDF000" : "#FF3B3B" }}
                aria-hidden="true"
              />
              <span className="text-[#D4D0C8] flex-1 truncate">{result.endpoint}</span>
              <span className={result.ok ? "text-[#BDF000]" : "text-[#FF3B3B]"}>
                {result.ms === null ? "timeout" : `${result.ms}ms`}
              </span>
            </div>
          ))}
          {lastChecked && (
            <div className="pt-3 text-[10px] text-slate-600 tracking-wider uppercase">
              checked {lastChecked.toLocaleTimeString()}
            </div>
          )}
        </div>
      )}
    </ChartCard>
  );
}

export default function Dashboard() {
  const { isGlobal } = useProject();

  const tasks = usePollingData<TaskManifest[]>("/api/tasks");
  const retros = usePollingData<TaskRetrospective[]>("/api/retrospectives");
  const agents = usePollingData<Array<{ route_allowed?: boolean; status?: string; last_benchmarked_task_offset?: number }>>("/api/agents");
  const autofix = usePollingData<AutofixMetrics>("/api/autofix-metrics");
  const report = usePollingData<RepoReport>("/api/report");
  const stats = usePollingData<RepoProjectStats>("/api/project-stats");
  const state = usePollingData<RepoState>("/api/state");

  const mostRecentTask = useMemo(() => {
    if (!tasks.data?.length) return null;
    return [...tasks.data].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())[0];
  }, [tasks.data]);

  const lastDoneTask = useMemo(() => {
    if (!tasks.data?.length) return null;
    const doneTasks = tasks.data
      .filter((task) => task.stage === "DONE")
      .sort((a, b) => {
        const aTime = new Date(a.completed_at ?? a.created_at).getTime();
        const bTime = new Date(b.completed_at ?? b.created_at).getTime();
        return bTime - aTime;
      });
    return doneTasks[0] ?? null;
  }, [tasks.data]);

  const lastDoneRetro = useMemo(() => {
    if (!lastDoneTask || !retros.data) return null;
    return retros.data.find((retro) => retro.task_id === lastDoneTask.task_id) ?? null;
  }, [lastDoneTask, retros.data]);

  const logUrl = mostRecentTask ? `/api/tasks/${mostRecentTask.task_id}/execution-log` : "";
  const execLog = usePollingData<ExecutionLogResponse>(logUrl || "/api/tasks/__none__/execution-log", logUrl ? 5000 : 999999);

  const isLoading = tasks.loading || retros.loading || agents.loading || autofix.loading || report.loading || stats.loading;
  const fatalError = tasks.error ?? retros.error ?? agents.error ?? autofix.error ?? report.error ?? stats.error;

  const activeTaskCount = useMemo(() => (tasks.data ?? []).filter((task) => task.stage !== "DONE").length, [tasks.data]);
  const completedTaskCount = useMemo(() => (tasks.data ?? []).filter((task) => task.stage === "DONE").length, [tasks.data]);
  const routeableAgentCount = useMemo(() => (agents.data ?? []).filter((agent) => Boolean(agent.route_allowed)).length, [agents.data]);
  const staleAgentCount = useMemo(
    () => (agents.data ?? []).filter((agent) => (agent.last_benchmarked_task_offset ?? 0) > 10).length,
    [agents.data],
  );
  const daemonActive = isDaemonActive(execLog.data?.lines ?? []);
  const autofixRateLimits = autofix.data?.rate_limits;
  const avgQuality = stats.data?.average_quality_score
    ?? ((retros.data?.length ?? 0) > 0
      ? (retros.data ?? []).reduce((sum, retro) => sum + retro.quality_score, 0) / (retros.data?.length ?? 1)
      : null);

  const summaryCards = [
    {
      label: "Active Tasks",
      value: activeTaskCount,
      icon: Activity,
      color: "#BDF000",
    },
    {
      label: "Completed Tasks",
      value: completedTaskCount,
      icon: CheckCircle2,
      color: "#2DD4A8",
    },
    {
      label: "Routeable Agents",
      value: routeableAgentCount,
      icon: GitBranch,
      color: "#B47AFF",
    },
    {
      label: "Autofix Findings",
      value: autofix.data?.totals.findings ?? 0,
      icon: Shield,
      color: "#FF9F43",
    },
    {
      label: "Avg Quality",
      value: avgQuality === null ? "--" : formatPercent(avgQuality),
      icon: Sparkles,
      color: "#2DD4A8",
    },
  ];

  const attentionItems = [
    {
      title: "Automation Queue",
      value: formatCount(report.data?.summary.queued_automation_jobs ?? 0),
      detail: `${report.data?.summary.queued_automation_jobs ?? 0} queued control-plane jobs waiting for automation or benchmark execution.`,
      tone: (report.data?.summary.queued_automation_jobs ?? 0) > 0 ? "warn" as const : "good" as const,
    },
    {
      title: "Coverage Gaps",
      value: formatCount(report.data?.summary.coverage_gaps ?? 0),
      detail: `${report.data?.summary.coverage_gaps ?? 0} learned components are missing benchmark fixtures and cannot be promoted confidently.`,
      tone: (report.data?.summary.coverage_gaps ?? 0) > 0 ? "warn" as const : "good" as const,
    },
    {
      title: "Stale Agents",
      value: formatCount(staleAgentCount),
      detail: `${staleAgentCount} learned agents are more than 10 task offsets behind on benchmarking freshness.`,
      tone: staleAgentCount > 0 ? "warn" as const : "good" as const,
    },
    {
      title: "Demotions",
      value: formatCount(report.data?.summary.demoted_components ?? 0),
      detail: `${report.data?.summary.demoted_components ?? 0} learned components are currently demoted on regression.`,
      tone: (report.data?.summary.demoted_components ?? 0) > 0 ? "bad" as const : "good" as const,
    },
  ];

  const activeRoutesRows = useMemo(
    () => (report.data?.active_routes ?? [])
      .slice()
      .sort((a, b) => b.composite - a.composite)
      .slice(0, 6)
      .map((item) => [item.agent_name, item.role, item.task_type, item.composite.toFixed(3)]),
    [report.data],
  );

  const demotionRows = useMemo(
    () => (report.data?.demotions ?? [])
      .slice(0, 6)
      .map((item) => [
        item.agent_name,
        item.role,
        item.task_type,
        typeof item.last_evaluation?.delta_composite === "number" ? item.last_evaluation.delta_composite.toFixed(3) : "n/a",
      ]),
    [report.data],
  );

  const coverageRows = useMemo(
    () => (report.data?.coverage_gaps ?? [])
      .slice(0, 6)
      .map((item) => [item.target_name, item.role, item.task_type, item.item_kind]),
    [report.data],
  );

  const findingsCategoryRows = useMemo(
    () => Object.entries(state.data?.recent_findings_by_category ?? {})
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .slice(0, 6)
      .map(([category, count]) => [category, count]),
    [state.data],
  );

  if (isLoading && !tasks.data && !retros.data && !agents.data && !autofix.data && !report.data) {
    return (
      <div className="p-6 space-y-6">
        <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-5">
              <Skeleton className="h-3 w-24 mb-4" />
              <Skeleton className="h-8 w-16" />
            </div>
          ))}
        </div>
        <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
          {Array.from({ length: 6 }).map((_, i) => (
            <div key={i} className="border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-5">
              <Skeleton className="h-4 w-32 mb-4" />
              <Skeleton className="h-32 w-full" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  if (fatalError && !tasks.data && !retros.data) {
    return (
      <div className="p-6">
        <ChartCard title="Dashboard Error" subtitle="The home page could not load its required data.">
          <div className="space-y-4">
            <p className="text-sm text-slate-400">{fatalError}</p>
            <button
              onClick={() => {
                tasks.refetch();
                retros.refetch();
                agents.refetch();
                autofix.refetch();
                report.refetch();
                stats.refetch();
                state.refetch();
              }}
              className="px-4 py-2 bg-[#BDF000]/10 hover:bg-[#BDF000]/20 border border-[#BDF000]/30 text-[#BDF000] font-mono text-xs rounded-xl transition-colors"
            >
              Retry
            </button>
          </div>
        </ChartCard>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-6">
      <div className="rounded-[28px] border border-white/6 bg-[radial-gradient(circle_at_top_left,_rgba(189,240,0,0.14),_transparent_38%),radial-gradient(circle_at_top_right,_rgba(180,122,255,0.14),_transparent_36%),linear-gradient(180deg,_#222222_0%,_#121212_100%)] p-6">
        <div className="flex items-start justify-between gap-6 flex-wrap">
          <div>
            <div className="flex items-center gap-3 mb-3">
              <StatusPill label={isGlobal ? "global overview" : "project overview"} color="#BDF000" />
              <StatusPill label={daemonActive ? "daemon active" : "daemon idle"} color={daemonActive ? "#2DD4A8" : "#FF9F43"} />
            </div>
            <h1 className="text-3xl sm:text-4xl font-mono font-light tracking-[0.14em] text-[#F0F0E8] uppercase">
              Command Center
            </h1>
            <p className="text-sm font-mono text-slate-400 mt-3 max-w-3xl leading-relaxed">
              Current task throughput, learned-system posture, autofix pressure, and repo health in one place.
            </p>
          </div>
          <div className="rounded-2xl border border-white/6 bg-black/20 px-4 py-3 min-w-[240px]">
            <div className="text-[10px] font-mono text-slate-500 uppercase tracking-wider mb-1">Latest Completed Task</div>
            <div className="text-sm font-mono text-slate-200 break-words">
              {lastDoneTask ? truncate(lastDoneTask.title, 48) : "No completed tasks"}
            </div>
            <div className="text-[10px] font-mono text-slate-600 mt-2">
              {lastDoneTask ? `${lastDoneTask.task_id} · ${relativeTime(lastDoneTask.completed_at ?? lastDoneTask.created_at)}` : "Waiting for first completion"}
            </div>
          </div>
        </div>
      </div>

      <div className="grid grid-cols-2 lg:grid-cols-5 gap-3">
        {summaryCards.map((card, index) => {
          const Icon = card.icon;
          return (
            <MetricCard
              key={card.label}
              label={card.label}
              value={card.value}
              trend={null}
              icon={<Icon className="w-3.5 h-3.5" style={{ color: card.color }} aria-hidden="true" />}
              delay={index * 0.05}
            />
          );
        })}
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-3 gap-6">
        <ChartCard title="Needs Attention" subtitle="The fastest read on current operator pressure.">
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            {attentionItems.map((item) => (
              <AttentionItem key={item.title} {...item} />
            ))}
          </div>
        </ChartCard>

        <ChartCard title="Control Plane" subtitle="Current daemon, scan, and learning-system posture.">
          <div className="space-y-3 font-mono text-xs">
            <div className="flex items-center justify-between border-b border-white/5 pb-2">
              <span className="text-slate-500 uppercase tracking-wider">Daemon</span>
              <span className={daemonActive ? "text-[#2DD4A8]" : "text-[#FF9F43]"}>{daemonActive ? "ACTIVE" : "IDLE"}</span>
            </div>
            <div className="flex items-center justify-between border-b border-white/5 pb-2">
              <span className="text-slate-500 uppercase tracking-wider">Last Autofix Scan</span>
              <span className="text-slate-300">{formatTimestamp(autofix.data?.generated_at)}</span>
            </div>
            <div className="flex items-center justify-between border-b border-white/5 pb-2">
              <span className="text-slate-500 uppercase tracking-wider">Learned Components</span>
              <span className="text-slate-300">{formatCount(report.data?.summary.learned_components ?? 0)}</span>
            </div>
            <div className="flex items-center justify-between border-b border-white/5 pb-2">
              <span className="text-slate-500 uppercase tracking-wider">Active Routes</span>
              <span className="text-slate-300">{formatCount(report.data?.summary.active_routes ?? 0)}</span>
            </div>
            <div className="flex items-center justify-between border-b border-white/5 pb-2">
              <span className="text-slate-500 uppercase tracking-wider">Tracked Fixtures</span>
              <span className="text-slate-300">{formatCount(report.data?.summary.tracked_fixtures ?? 0)}</span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-slate-500 uppercase tracking-wider">Benchmark Runs</span>
              <span className="text-slate-300">{formatCount(report.data?.summary.benchmark_runs ?? 0)}</span>
            </div>
          </div>
        </ChartCard>

        <ChartCard title="Repo Snapshot" subtitle={isGlobal ? "Codebase state is shown per-project only." : "Current codebase-state snapshot from repo metrics."}>
          {isGlobal || !state.data ? (
            <div className="space-y-3 font-mono text-xs">
              <div className="flex items-center gap-2 text-slate-500">
                <FileCode2 className="w-4 h-4" aria-hidden="true" />
                Repo state is unavailable in global mode.
              </div>
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-3">
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-slate-500 uppercase tracking-wider">Files</div>
                <div className="text-xl font-mono text-slate-200 mt-2">{formatCount(state.data.file_count)}</div>
              </div>
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-slate-500 uppercase tracking-wider">Lines</div>
                <div className="text-xl font-mono text-slate-200 mt-2">{formatCount(state.data.line_count)}</div>
              </div>
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-slate-500 uppercase tracking-wider">Complexity</div>
                <div className="text-xl font-mono text-slate-200 mt-2">{state.data.architecture_complexity_score.toFixed(2)}</div>
              </div>
              <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                <div className="text-[10px] text-slate-500 uppercase tracking-wider">Dependency Flux</div>
                <div className="text-xl font-mono text-slate-200 mt-2">{state.data.dependency_flux.toFixed(2)}</div>
              </div>
            </div>
          )}
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard title="Recent Activity" subtitle="Execution log tail from the most recent task in scope.">
          {execLog.loading ? (
            <div className="space-y-3" role="status" aria-label="Loading recent activity">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-4 bg-white/5" style={{ width: `${70 + i * 4}%` }} />
              ))}
            </div>
          ) : execLog.error ? (
            <p className="text-slate-500 font-mono text-xs">Unable to load execution log.</p>
          ) : (
            <FeedCard lines={execLog.data?.lines ?? []} />
          )}
        </ChartCard>

        <ChartCard title="Last Completed Task" subtitle="Most recent finished task and its final retrospective outcome.">
          {lastDoneTask ? (
            <div className="space-y-3 font-mono text-xs">
              <div className="text-slate-200 text-sm leading-relaxed">{lastDoneTask.title}</div>
              <div className="flex items-center gap-2 flex-wrap">
                <StatusPill label={lastDoneTask.task_id} color="#B47AFF" />
                <StatusPill label={lastDoneTask.classification.type || "unknown"} color="#2DD4A8" />
                <StatusPill label={lastDoneTask.classification.risk_level || "unknown"} color="#FF9F43" />
              </div>
              <div className="grid grid-cols-2 gap-3 pt-2">
                <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                  <div className="text-[10px] text-slate-500 uppercase tracking-wider">Completed</div>
                  <div className="text-slate-300 mt-2">{relativeTime(lastDoneTask.completed_at ?? lastDoneTask.created_at)}</div>
                </div>
                <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                  <div className="text-[10px] text-slate-500 uppercase tracking-wider">Quality</div>
                  <div className="text-slate-300 mt-2">{lastDoneRetro ? formatPercent(lastDoneRetro.quality_score) : "--"}</div>
                </div>
                <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                  <div className="text-[10px] text-slate-500 uppercase tracking-wider">Repair Cycles</div>
                  <div className="text-slate-300 mt-2">{lastDoneRetro?.repair_cycle_count ?? "--"}</div>
                </div>
                <div className="rounded-xl border border-white/6 bg-black/20 p-4">
                  <div className="text-[10px] text-slate-500 uppercase tracking-wider">Tokens</div>
                  <div className="text-slate-300 mt-2">{lastDoneRetro ? formatCount(lastDoneRetro.total_token_usage) : "--"}</div>
                </div>
              </div>
            </div>
          ) : (
            <p className="text-slate-500 font-mono text-xs">No completed tasks yet.</p>
          )}
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard title="Active Routes" subtitle="Highest-performing currently routeable learned components.">
          <TableBlock headers={["Agent", "Role", "Task Type", "Composite"]} rows={activeRoutesRows} empty="No active routes available." />
        </ChartCard>
        <ChartCard title="Recent Demotions" subtitle="Components demoted on regression and their last composite delta.">
          <TableBlock headers={["Agent", "Role", "Task Type", "Delta"]} rows={demotionRows} empty="No demotions recorded." />
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <ChartCard title="Coverage Gaps" subtitle="Learned components currently missing benchmark fixtures.">
          <TableBlock headers={["Target", "Role", "Task Type", "Kind"]} rows={coverageRows} empty="No benchmark coverage gaps detected." />
        </ChartCard>
        <ChartCard title="Recent Findings by Category" subtitle="Most recent repo-level finding concentration from state encoding.">
          <TableBlock headers={["Category", "Findings"]} rows={findingsCategoryRows} empty="No recent finding categories recorded." />
        </ChartCard>
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-2 gap-6">
        <HealthCheckCard />
        <ChartCard title="Autofix Pressure" subtitle="Current rate limits and live autofix pressure indicators.">
          <div className="grid grid-cols-2 gap-3">
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-slate-500 uppercase tracking-wider">Open PRs</div>
              <div className="text-xl font-mono text-slate-200 mt-2">
                {autofixRateLimits ? `${autofixRateLimits.open_prs}/${autofixRateLimits.max_open_prs}` : "--"}
              </div>
            </div>
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-slate-500 uppercase tracking-wider">PRs Today</div>
              <div className="text-xl font-mono text-slate-200 mt-2">
                {autofixRateLimits ? `${autofixRateLimits.prs_today}/${autofixRateLimits.max_prs_per_day}` : "--"}
              </div>
            </div>
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-slate-500 uppercase tracking-wider">Recent Failures</div>
              <div className="text-xl font-mono text-slate-200 mt-2">{autofix.data?.totals.recent_failures ?? "--"}</div>
            </div>
            <div className="rounded-xl border border-white/6 bg-black/20 p-4">
              <div className="text-[10px] text-slate-500 uppercase tracking-wider">Suppressions</div>
              <div className="text-xl font-mono text-slate-200 mt-2">{autofix.data?.totals.suppression_count ?? "--"}</div>
            </div>
          </div>
        </ChartCard>
      </div>
    </div>
  );
}
