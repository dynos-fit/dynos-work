import { useState, useCallback, useEffect, useMemo } from "react";
import { Link } from "react-router";
import { motion } from "motion/react";
import { Search, Terminal, ChevronDown, ChevronRight, ChevronUp, AlertCircle, FileText, GitBranch, Shield, DollarSign, ListChecks, Play, CheckCircle2, XCircle, ExternalLink } from "lucide-react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { usePollingData } from "@/data/hooks";
import { useProject } from "@/data/ProjectContext";
import type { TaskManifest, TaskRetrospective, ExecutionGraph, ExecutionSegment, AuditReport, AuditFinding } from "@/data/types";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricCard } from "@/components/MetricCard";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from "@/components/ui/collapsible";

// ---- Constants ----

const STAGE_COLORS: Record<string, string> = {
  DONE: "#2DD4A8",
  FAILED: "#FF3B3B",
  BLOCKED: "#FF3B3B",
};

const RISK_COLORS: Record<string, string> = {
  low: "#BDF000",
  medium: "#2DD4A8",
  high: "#B47AFF",
  critical: "#FF3B3B",
};

const EXECUTOR_COLORS: Record<string, string> = {
  ui: "#BDF000",
  backend: "#2DD4A8",
  ml: "#B47AFF",
  test: "#FF9F43",
  infra: "#FF3B3B",
  db: "#FF7043",
};

const SEVERITY_COLORS: Record<string, string> = {
  critical: "#FF3B3B",
  high: "#B47AFF",
  medium: "#FF9F43",
  low: "#BDF000",
};

const TIMELINE_STAGES = ["DISCOVERY", "SPEC_REVIEW", "PLANNING", "PLAN_REVIEW", "EXECUTION", "AUDITING", "DONE"] as const;

// Map actual manifest stages to timeline position
const STAGE_TO_TIMELINE: Record<string, number> = {
  FOUNDRY_INITIALIZED: 0,
  DISCOVERY: 0,
  SPEC_NORMALIZATION: 1,
  SPEC_REVIEW: 1,
  PLANNING: 2,
  PLAN_REVIEW: 3,
  PLAN_AUDIT: 3,
  PRE_EXECUTION_SNAPSHOT: 4,
  EXECUTION: 4,
  TEST_EXECUTION: 4,
  CHECKPOINT_AUDIT: 5,
  AUDITING: 5,
  FINAL_AUDIT: 5,
  DONE: 6,
};

const TIMELINE_STAGE_COLORS: Record<string, string> = {
  DISCOVERY: "#B47AFF",
  SPEC_REVIEW: "#BDF000",
  PLANNING: "#FF9F43",
  PLAN_REVIEW: "#FF7043",
  EXECUTION: "#2DD4A8",
  AUDITING: "#BDF000",
  DONE: "#2DD4A8",
  FAILED: "#FF3B3B",
  BLOCKED: "#FF3B3B",
};

const IN_PROGRESS_COLOR = "#FF9F43";
const FALLBACK_RISK_COLOR = "#999";

// ---- Project-scoped fetch helper ----

function useProjectFetchUrl() {
  const { selectedProject } = useProject();
  return (path: string) => {
    const sep = path.includes("?") ? "&" : "?";
    return `${path}${sep}project=${encodeURIComponent(selectedProject)}`;
  };
}

// ---- Helpers ----

function getStageColor(stage: string): string {
  if (stage === "DONE") return STAGE_COLORS.DONE;
  if (stage.includes("FAIL")) return STAGE_COLORS.FAILED;
  if (stage.includes("BLOCKED")) return STAGE_COLORS.BLOCKED;
  return IN_PROGRESS_COLOR;
}

function getRiskColor(risk: string): string {
  return RISK_COLORS[risk] ?? FALLBACK_RISK_COLOR;
}

function basename(path: string): string {
  return path.split("/").pop() ?? path;
}

function formatQualityScore(retros: TaskRetrospective[] | null, taskId: string): string {
  if (!retros) return "--";
  const retro = retros.find((r) => r.task_id === taskId);
  if (!retro || retro.quality_score === undefined) return "--";
  return Math.round(retro.quality_score * 100) + "%";
}

function getExecutorColor(executor: string): string {
  const lower = executor.toLowerCase();
  for (const [key, color] of Object.entries(EXECUTOR_COLORS)) {
    if (lower.includes(key)) return color;
  }
  return "#999";
}

function getSeverityColor(severity: string): string {
  return SEVERITY_COLORS[severity.toLowerCase()] ?? "#999";
}

function getTimelineProgress(stage: string): number {
  if (stage.includes("FAIL") || stage.includes("BLOCKED")) return -1;
  const mapped = STAGE_TO_TIMELINE[stage];
  if (mapped !== undefined) return mapped;
  // Fallback: try direct match
  const idx = TIMELINE_STAGES.indexOf(stage as typeof TIMELINE_STAGES[number]);
  return idx === -1 ? 0 : idx;
}

// ---- Sub-components ----

function SkeletonTable({ rows }: { rows: number }) {
  return (
    <div className="space-y-3" role="status" aria-label="Loading tasks">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className="flex gap-4 px-4 py-3">
          <Skeleton className="h-4 w-36" />
          <Skeleton className="h-4 w-48" />
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-4 w-20" />
          <Skeleton className="h-4 w-24" />
        </div>
      ))}
    </div>
  );
}

function ErrorCard({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      className="flex flex-col items-center justify-center py-16 px-4 bg-red-500/10 border border-red-500/30 rounded-lg"
      role="alert"
    >
      <p className="text-red-400 font-mono text-sm mb-4">
        Unable to load tasks. Please try again.
      </p>
      <p className="text-slate-500 font-mono text-xs mb-6 max-w-md text-center truncate">
        {message}
      </p>
      <button
        onClick={onRetry}
        className="px-4 py-2 bg-red-500/20 hover:bg-red-500/30 text-red-400 border border-red-500/30 font-mono text-xs rounded transition-colors"
        aria-label="Retry loading tasks"
      >
        RETRY
      </button>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-20 px-4" role="status">
      <Terminal className="w-10 h-10 text-slate-600 mb-4" aria-hidden="true" />
      <p className="text-slate-400 font-mono text-sm">No tasks found</p>
      <p className="text-slate-600 font-mono text-xs mt-2">
        Tasks will appear here once created via the CLI.
      </p>
    </div>
  );
}

function StaleErrorBanner({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div
      className="flex items-center justify-between gap-4 px-4 py-2 mb-4 bg-red-500/10 border border-red-500/30 rounded text-xs font-mono"
      role="alert"
    >
      <span className="text-red-400 truncate">Update failed: {message}</span>
      <button
        onClick={onRetry}
        className="text-red-400 hover:text-red-300 underline shrink-0"
        aria-label="Retry loading tasks"
      >
        Retry
      </button>
    </div>
  );
}

// ---- AC-14: Timeline indicator ----

function TimelineIndicator({ stage }: { stage: string }) {
  const progress = getTimelineProgress(stage);
  const isFailed = progress === -1;
  const totalStages = TIMELINE_STAGES.length;

  return (
    <div
      className="flex items-center gap-px h-3 w-20"
      aria-label={`Stage progression: ${stage}`}
      title={stage}
    >
      {TIMELINE_STAGES.map((s, i) => {
        let bgColor: string;
        if (isFailed) {
          bgColor = i === 0 ? "#FF3B3B" : "#333";
        } else if (i <= progress) {
          bgColor = TIMELINE_STAGE_COLORS[s] ?? IN_PROGRESS_COLOR;
        } else {
          bgColor = "#333";
        }
        const isFirst = i === 0;
        const isLast = i === totalStages - 1;
        return (
          <div
            key={s}
            className={`h-full flex-1 ${isFirst ? "rounded-l" : ""} ${isLast ? "rounded-r" : ""}`}
            style={{ backgroundColor: bgColor }}
            aria-hidden="true"
          />
        );
      })}
    </div>
  );
}

// ---- AC-11: Markdown section ----

function MarkdownSection({ taskId, endpoint, label }: { taskId: string; endpoint: string; label: string }) {
  const [content, setContent] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const projectUrl = useProjectFetchUrl();

  useEffect(() => {
    if (!open || content !== null) return;
    let cancelled = false;

    async function fetchContent() {
      setLoading(true);
      try {
        const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/${endpoint}`));
        if (!res.ok) throw new Error(`Failed to load ${label.toLowerCase()}`);
        const data = await res.json() as { content?: string; markdown?: string; text?: string };
        if (!cancelled) {
          setContent(data.content ?? data.markdown ?? data.text ?? "");
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchContent();
    return () => { cancelled = true; };
  }, [open, taskId, endpoint, label, content]);

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <button
          className="flex items-center gap-2 text-slate-400 hover:text-slate-200 font-mono text-xs transition-colors"
          aria-expanded={open}
          aria-label={`Toggle ${label} section`}
        >
          {open ? (
            <ChevronDown className="w-3 h-3" aria-hidden="true" />
          ) : (
            <ChevronRight className="w-3 h-3" aria-hidden="true" />
          )}
          <FileText className="w-3 h-3" aria-hidden="true" />
          {label}
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="mt-2 bg-black/40 rounded p-3 max-h-64 overflow-auto font-mono text-xs">
          {loading && (
            <div className="space-y-2" role="status" aria-label={`Loading ${label.toLowerCase()}`}>
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-3/4" />
              <Skeleton className="h-3 w-1/2" />
            </div>
          )}
          {error && (
            <p className="text-red-400" role="alert">{error}</p>
          )}
          {content !== null && !loading && !error && content.length === 0 && (
            <p className="text-slate-600">No {label.toLowerCase()} content available.</p>
          )}
          {content !== null && !loading && !error && content.length > 0 && (
            <div className="prose prose-invert prose-xs max-w-none text-slate-300 [&_pre]:bg-black/30 [&_pre]:p-2 [&_pre]:rounded [&_code]:text-[#BDF000] [&_h1]:text-sm [&_h2]:text-xs [&_h3]:text-xs [&_table]:text-xs [&_a]:text-[#BDF000]">
              <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
                {content}
              </ReactMarkdown>
            </div>
          )}
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

// ---- AC-12: Execution graph mini-visualization ----

function ExecutionGraphMini({ segments }: { segments: ExecutionSegment[] }) {
  if (segments.length === 0) {
    return <p className="text-slate-600 font-mono text-xs">No segments defined.</p>;
  }

  // Group by dependency depth for layout
  const depthMap = new Map<string, number>();
  function getDepth(seg: ExecutionSegment): number {
    if (depthMap.has(seg.id)) return depthMap.get(seg.id)!;
    if (seg.depends_on.length === 0) {
      depthMap.set(seg.id, 0);
      return 0;
    }
    const parentDepths = seg.depends_on.map((depId) => {
      const parent = segments.find((s) => s.id === depId);
      return parent ? getDepth(parent) : 0;
    });
    const depth = Math.max(...parentDepths) + 1;
    depthMap.set(seg.id, depth);
    return depth;
  }
  segments.forEach(getDepth);

  const maxDepth = Math.max(...Array.from(depthMap.values()));
  const layers: ExecutionSegment[][] = Array.from({ length: maxDepth + 1 }, () => []);
  for (const seg of segments) {
    layers[depthMap.get(seg.id)!].push(seg);
  }

  return (
    <div
      className="flex items-start gap-2 overflow-x-auto pb-2"
      role="img"
      aria-label={`Execution graph with ${segments.length} segments across ${layers.length} layers`}
    >
      {layers.map((layer, layerIdx) => (
        <div key={layerIdx} className="flex flex-col gap-1 items-center shrink-0">
          {layer.map((seg) => {
            const color = getExecutorColor(seg.executor);
            return (
              <div
                key={seg.id}
                className="px-2 py-1 rounded border text-[10px] font-mono whitespace-nowrap max-w-[120px] truncate"
                style={{
                  borderColor: color + "4D",
                  backgroundColor: color + "1A",
                  color: color,
                }}
                title={`${seg.id}: ${seg.description}`}
              >
                {seg.id}
              </div>
            );
          })}
          {layerIdx < layers.length - 1 && (
            <div className="text-slate-600 text-xs" aria-hidden="true">
              &#8594;
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ---- AC-13: Audit findings summary ----

function AuditFindingsSummary({ taskId }: { taskId: string }) {
  const [reports, setReports] = useState<AuditReport[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const projectUrl = useProjectFetchUrl();

  useEffect(() => {
    let cancelled = false;

    async function fetchReports() {
      setLoading(true);
      try {
        const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/audit-reports`));
        if (!res.ok) throw new Error("Failed to load audit reports");
        const data = (await res.json()) as AuditReport[];
        if (!cancelled) {
          setReports(data);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchReports();
    return () => { cancelled = true; };
  }, [taskId]);

  const allFindings: AuditFinding[] = useMemo(() => {
    if (!reports) return [];
    return reports.flatMap((r) => r.findings);
  }, [reports]);

  return (
    <div>
      <h4 className="text-slate-500 font-mono text-[10px] uppercase tracking-wider mb-2 flex items-center gap-1.5">
        <Shield className="w-3 h-3" aria-hidden="true" />
        Audit Findings
      </h4>
      {loading && (
        <div className="space-y-1" role="status" aria-label="Loading audit findings">
          <Skeleton className="h-3 w-64" />
          <Skeleton className="h-3 w-48" />
        </div>
      )}
      {error && (
        <p className="text-red-400 font-mono text-xs" role="alert">{error}</p>
      )}
      {reports !== null && !loading && !error && allFindings.length === 0 && (
        <p className="text-slate-600 font-mono text-xs">No audit findings recorded.</p>
      )}
      {reports !== null && !loading && !error && allFindings.length > 0 && (
        <div className="space-y-1 max-h-40 overflow-y-auto">
          {allFindings.map((finding) => {
            const color = getSeverityColor(finding.severity);
            return (
              <div key={finding.id} className="flex items-center gap-2 text-xs font-mono">
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: color }}
                  aria-label={`${finding.severity} severity`}
                />
                <span className="text-slate-300 truncate max-w-md" title={finding.description}>
                  {finding.title || finding.description}
                </span>
                <span className="text-slate-600 shrink-0">{finding.category}</span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---- Token Usage Summary per task ----

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

const PHASE_COLORS: Record<string, string> = {
  planning: "#B47AFF",
  execution: "#2DD4A8",
  audit: "#BDF000",
  tdd: "#FF9F43",
  repair: "#FF3B3B",
};

const EVENT_TYPE_STYLES: Record<string, { color: string; label: string }> = {
  spawn: { color: "#00E5FF", label: "SPAWN" },
  deterministic: { color: "#FFB300", label: "DETERMINISTIC" },
  inline: { color: "#FF9F43", label: "INLINE" },
};

const MODEL_BADGE_COLORS: Record<string, string> = {
  opus: "#7C4DFF",
  sonnet: "#00E5FF",
  haiku: "#00BFA5",
  none: "#555",
};

interface TokenEvent {
  timestamp: string;
  agent: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  tokens: number;
  phase: string;
  stage: string;
  type: string;
  segment?: string;
  detail?: string;
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso.slice(11, 19);
  }
}

function Badge({ label, color }: { label: string; color: string }) {
  return (
    <span
      className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider shrink-0"
      style={{ color, borderColor: color + "4D", backgroundColor: color + "1A", border: `1px solid ${color}4D` }}
    >
      {label}
    </span>
  );
}

function TokenUsageSummary({ taskId }: { taskId: string }) {
  const [data, setData] = useState<Record<string, unknown> | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showEvents, setShowEvents] = useState(false);
  const projectUrl = useProjectFetchUrl();

  useEffect(() => {
    let cancelled = false;

    async function fetchTokens() {
      setLoading(true);
      try {
        const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/token-usage`));
        if (!res.ok) throw new Error("No token data");
        const json = await res.json();
        if (!cancelled) {
          setData(json as Record<string, unknown>);
          setError(null);
        }
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    fetchTokens();
    return () => { cancelled = true; };
  }, [taskId]);

  const byAgent = data?.by_agent as Record<string, { input_tokens: number; output_tokens: number; tokens: number; model: string }> | undefined;
  const events = (data?.events ?? []) as TokenEvent[];
  const totalInput = (data?.total_input_tokens as number) ?? 0;
  const totalOutput = (data?.total_output_tokens as number) ?? 0;
  const total = (data?.total as number) ?? 0;

  return (
    <div>
      <h4 className="text-slate-500 font-mono text-[10px] uppercase tracking-wider mb-2 flex items-center gap-1.5">
        <DollarSign className="w-3 h-3" aria-hidden="true" />
        Token Usage
      </h4>
      {loading && (
        <div className="space-y-1" role="status" aria-label="Loading token usage">
          <Skeleton className="h-3 w-64" />
          <Skeleton className="h-3 w-48" />
        </div>
      )}
      {error && (
        <p className="text-slate-600 font-mono text-xs">No token usage data recorded.</p>
      )}
      {data && !loading && !error && (
        <div className="space-y-3">
          {/* Aggregate table by agent */}
          {byAgent && Object.keys(byAgent).length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full font-mono text-xs" aria-label="Token usage by agent">
                <thead>
                  <tr className="border-b border-white/10">
                    <th className="text-left text-slate-500 py-1 pr-3">Agent</th>
                    <th className="text-left text-slate-500 py-1 pr-3">Model</th>
                    <th className="text-right text-slate-500 py-1 pr-3">Input</th>
                    <th className="text-right text-slate-500 py-1 pr-3">Output</th>
                    <th className="text-right text-slate-500 py-1">Total</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(byAgent)
                    .sort(([, a], [, b]) => b.tokens - a.tokens)
                    .map(([agent, info]) => (
                    <tr key={agent} className="border-b border-white/5">
                      <td className="text-slate-300 py-1 pr-3 max-w-[180px] truncate" title={agent}>{agent}</td>
                      <td className="py-1 pr-3">
                        <span className="text-[10px] font-mono font-medium" style={{ color: MODEL_BADGE_COLORS[info.model] ?? "#999" }}>
                          {info.model}
                        </span>
                      </td>
                      <td className="text-right text-[#7C4DFF] py-1 pr-3">{formatTokens(info.input_tokens)}</td>
                      <td className="text-right text-[#00E5FF] py-1 pr-3">{formatTokens(info.output_tokens)}</td>
                      <td className="text-right text-slate-400 py-1">{formatTokens(info.tokens)}</td>
                    </tr>
                  ))}
                </tbody>
                <tfoot>
                  <tr className="border-t border-[#00E5FF]/20">
                    <td colSpan={2} className="text-slate-300 font-semibold py-1 pr-3">Total</td>
                    <td className="text-right text-[#7C4DFF] font-semibold py-1 pr-3">{formatTokens(totalInput)}</td>
                    <td className="text-right text-[#00E5FF] font-semibold py-1 pr-3">{formatTokens(totalOutput)}</td>
                    <td className="text-right text-slate-300 font-semibold py-1">{formatTokens(total)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          )}

          {/* Event log toggle */}
          {events.length > 0 && (
            <Collapsible open={showEvents} onOpenChange={setShowEvents}>
              <CollapsibleTrigger asChild>
                <button
                  className="flex items-center gap-1.5 text-slate-400 hover:text-slate-200 font-mono text-[10px] uppercase tracking-wider transition-colors"
                  aria-expanded={showEvents}
                >
                  {showEvents ? (
                    <ChevronDown className="w-3 h-3" aria-hidden="true" />
                  ) : (
                    <ChevronRight className="w-3 h-3" aria-hidden="true" />
                  )}
                  Event Log ({events.length} events)
                </button>
              </CollapsibleTrigger>
              <CollapsibleContent>
                <div className="mt-2 max-h-80 overflow-y-auto bg-black/30 rounded p-2">
                  {events.map((evt, i) => {
                    const typeStyle = EVENT_TYPE_STYLES[evt.type] ?? { color: "#666", label: evt.type };
                    const phaseColor = PHASE_COLORS[evt.phase] ?? "#666";
                    const modelColor = MODEL_BADGE_COLORS[evt.model] ?? "#555";

                    return (
                      <div
                        key={i}
                        className="flex items-center gap-2 py-1.5 border-b border-white/5 last:border-0 flex-wrap"
                      >
                        {/* Timestamp */}
                        <span className="text-slate-600 text-[10px] font-mono shrink-0 w-[52px]">
                          {formatTime(evt.timestamp)}
                        </span>

                        {/* Type badge */}
                        <Badge label={typeStyle.label} color={typeStyle.color} />

                        {/* Phase badge */}
                        <Badge label={evt.phase} color={phaseColor} />

                        {/* Stage */}
                        {evt.stage && (
                          <span className="text-slate-600 text-[9px] font-mono shrink-0">
                            {evt.stage}
                          </span>
                        )}

                        {/* Agent */}
                        <span className="text-slate-300 text-[11px] font-mono shrink-0 max-w-[200px] truncate" title={evt.agent}>
                          {evt.agent}
                        </span>

                        {/* Model */}
                        {evt.model !== "none" && (
                          <span className="text-[10px] font-mono font-medium shrink-0" style={{ color: modelColor }}>
                            {evt.model}
                          </span>
                        )}

                        {/* Segment */}
                        {evt.segment && (
                          <span className="text-slate-600 text-[10px] font-mono shrink-0">
                            [{evt.segment}]
                          </span>
                        )}

                        {/* Tokens (only for non-zero) */}
                        {evt.tokens > 0 && (
                          <span className="text-[10px] font-mono shrink-0">
                            <span className="text-[#7C4DFF]">{formatTokens(evt.input_tokens)}</span>
                            <span className="text-slate-600">{" / "}</span>
                            <span className="text-[#00E5FF]">{formatTokens(evt.output_tokens)}</span>
                          </span>
                        )}

                        {/* Detail */}
                        {evt.detail && (
                          <span className="text-slate-500 text-[10px] font-mono truncate max-w-[300px]" title={evt.detail}>
                            {evt.detail}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              </CollapsibleContent>
            </Collapsible>
          )}

          {(!byAgent || Object.keys(byAgent).length === 0) && events.length === 0 && (
            <p className="text-slate-600 font-mono text-xs">No token usage data available.</p>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Row expansion detail ----

interface ExpandedDetailProps {
  taskId: string;
}

function ExpandedDetail({ taskId }: ExpandedDetailProps) {
  const [graph, setGraph] = useState<ExecutionGraph | null>(null);
  const [graphLoading, setGraphLoading] = useState(false);
  const [graphError, setGraphError] = useState<string | null>(null);

  const [logLines, setLogLines] = useState<string[] | null>(null);
  const [logLoading, setLogLoading] = useState(false);
  const [logError, setLogError] = useState<string | null>(null);
  const projectUrl = useProjectFetchUrl();

  useEffect(() => {
    let cancelled = false;

    async function fetchGraph() {
      setGraphLoading(true);
      try {
        const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/execution-graph`));
        if (!res.ok) throw new Error("Failed to load execution graph");
        const data = (await res.json()) as ExecutionGraph;
        if (!cancelled) {
          setGraph(data);
          setGraphError(null);
        }
      } catch (err) {
        if (!cancelled) setGraphError(err instanceof Error ? err.message : "Failed to load");
      } finally {
        if (!cancelled) setGraphLoading(false);
      }
    }

    async function fetchLog() {
      setLogLoading(true);
      try {
        const res = await fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskId)}/execution-log`));
        if (!res.ok) throw new Error("Failed to load execution log");
        const data = (await res.json()) as { lines: string[] };
        if (!cancelled) {
          setLogLines((data.lines ?? []).slice(-10));
          setLogError(null);
        }
      } catch (err) {
        if (!cancelled) setLogError(err instanceof Error ? err.message : "Failed to load");
      } finally {
        if (!cancelled) setLogLoading(false);
      }
    }

    fetchGraph();
    fetchLog();

    return () => {
      cancelled = true;
    };
  }, [taskId]);

  return (
    <div className="px-6 py-4 bg-[#0F1114]/40 border-t border-white/5 space-y-4">
      {/* AC-11: Spec and Plan markdown sections */}
      <div className="flex gap-6">
        <MarkdownSection taskId={taskId} endpoint="spec" label="SPEC" />
        <MarkdownSection taskId={taskId} endpoint="plan" label="PLAN" />
      </div>

      {/* AC-12: Execution Graph Mini-Visualization */}
      <div>
        <h4 className="text-slate-500 font-mono text-[10px] uppercase tracking-wider mb-2 flex items-center gap-1.5">
          <GitBranch className="w-3 h-3" aria-hidden="true" />
          Execution Graph
        </h4>
        {graphLoading && (
          <div className="space-y-2" role="status" aria-label="Loading execution graph">
            <Skeleton className="h-3 w-64" />
            <Skeleton className="h-3 w-48" />
          </div>
        )}
        {graphError && (
          <p className="text-red-400 font-mono text-xs" role="alert">{graphError}</p>
        )}
        {graph && <ExecutionGraphMini segments={graph.segments} />}
      </div>

      {/* AC-13: Audit Findings Summary */}
      <AuditFindingsSummary taskId={taskId} />

      {/* Token Usage per agent */}
      <TokenUsageSummary taskId={taskId} />

      {/* Execution Log */}
      <div>
        <h4 className="text-slate-500 font-mono text-[10px] uppercase tracking-wider mb-2">
          Execution Log (last 10 lines)
        </h4>
        {logLoading && (
          <div className="space-y-1" role="status" aria-label="Loading execution log">
            <Skeleton className="h-3 w-full" />
            <Skeleton className="h-3 w-3/4" />
          </div>
        )}
        {logError && (
          <p className="text-red-400 font-mono text-xs" role="alert">{logError}</p>
        )}
        {logLines && logLines.length > 0 && (
          <pre className="text-slate-400 font-mono text-[11px] leading-relaxed bg-black/30 rounded p-3 overflow-x-auto max-h-40">
            {logLines.join("\n")}
          </pre>
        )}
        {logLines && logLines.length === 0 && (
          <p className="text-slate-600 font-mono text-xs">No log output available.</p>
        )}
      </div>
    </div>
  );
}

// ---- Task Row ----

interface TaskRowProps {
  task: TaskManifest;
  qualityDisplay: string;
  isGlobal: boolean;
  index: number;
}

function TaskRow({ task, qualityDisplay, isGlobal, index }: TaskRowProps) {
  const [open, setOpen] = useState(false);
  const stageColor = getStageColor(task.stage);
  const riskColor = getRiskColor(task.classification?.risk_level);
  const colSpan = isGlobal ? 9 : 8;

  return (
    <Collapsible asChild open={open} onOpenChange={setOpen}>
      <>
        <CollapsibleTrigger asChild>
          <motion.tr
            initial={{ opacity: 0, x: -10 }}
            animate={{ opacity: 1, x: 0 }}
            transition={{ delay: index * 0.1 }}
            className={`border-b border-white/5 hover:bg-white/[0.04] transition-colors cursor-pointer ${index % 2 === 0 ? "" : "bg-white/[0.02]"}`}
            role="row"
            aria-expanded={open}
            aria-label={`Task ${task.task_id}: ${task.title}`}
            tabIndex={0}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                setOpen(!open);
              }
            }}
          >
            {isGlobal && (
              <td className="p-4 text-slate-400 font-mono text-xs">
                {task.project_path ? basename(task.project_path) : "--"}
              </td>
            )}
            <td className="p-4 text-[#BDF000] font-mono text-xs whitespace-nowrap">
              <span className="inline-flex items-center gap-1.5">
                {open ? (
                  <ChevronDown className="w-3 h-3 text-slate-500" aria-hidden="true" />
                ) : (
                  <ChevronRight className="w-3 h-3 text-slate-500" aria-hidden="true" />
                )}
                <Link
                  to={`/tasks/${task.task_id}`}
                  className="hover:underline hover:text-[#d4ff4d] transition-colors inline-flex items-center gap-1"
                  onClick={(e) => e.stopPropagation()}
                  title="Open task detail page"
                >
                  {task.task_id}
                  <ExternalLink className="w-2.5 h-2.5 opacity-50" aria-hidden="true" />
                </Link>
              </span>
            </td>
            <td className="p-4 text-slate-300 text-sm max-w-xs truncate">
              {task.title}
            </td>
            <td className="p-4">
              <span
                className="font-mono text-[10px] font-medium rounded-full px-2.5 py-0.5"
                style={{
                  color: stageColor,
                  backgroundColor: stageColor + "1A",
                }}
              >
                {task.stage}
              </span>
            </td>
            <td className="p-4 text-slate-400 text-xs">
              {task.classification?.type ?? "\u2014"}
            </td>
            <td className="p-4">
              <span
                className="font-mono text-[10px] rounded-full px-2.5 py-0.5"
                style={{
                  color: riskColor,
                  backgroundColor: riskColor + "1A",
                }}
              >
                {task.classification?.risk_level ?? "\u2014"}
              </span>
            </td>
            <td className="p-4 text-slate-300 font-mono text-xs">
              {qualityDisplay}
            </td>
            {/* AC-14: Timeline column */}
            <td className="p-4">
              <TimelineIndicator stage={task.stage} />
            </td>
            <td className="p-4">
              <span
                className="rounded-full px-2.5 py-0.5 text-[10px] border font-mono"
                style={{
                  color: stageColor,
                  borderColor: stageColor + "4D",
                  backgroundColor: stageColor + "1A",
                }}
              >
                {task.blocked_reason ? "BLOCKED" : task.stage}
              </span>
            </td>
          </motion.tr>
        </CollapsibleTrigger>
        <CollapsibleContent asChild>
          <tr className="border-b border-white/5">
            <td colSpan={colSpan} className="p-0">
              <ExpandedDetail taskId={task.task_id} />
            </td>
          </tr>
        </CollapsibleContent>
      </>
    </Collapsible>
  );
}

// ---- AC-15: Compare Tab ----

function CompareTab({ tasks, retrospectives }: { tasks: TaskManifest[]; retrospectives: TaskRetrospective[] | null }) {
  const [taskAId, setTaskAId] = useState("");
  const [taskBId, setTaskBId] = useState("");
  const [retroA, setRetroA] = useState<TaskRetrospective | null>(null);
  const [retroB, setRetroB] = useState<TaskRetrospective | null>(null);
  const [loadingA, setLoadingA] = useState(false);
  const [loadingB, setLoadingB] = useState(false);
  const [errorA, setErrorA] = useState<string | null>(null);
  const [errorB, setErrorB] = useState<string | null>(null);
  const projectUrl = useProjectFetchUrl();

  const taskOptions = useMemo(() => tasks.map((t) => t.task_id), [tasks]);

  useEffect(() => {
    if (!taskAId) { setRetroA(null); return; }
    // Try local retrospectives first
    const local = retrospectives?.find((r) => r.task_id === taskAId);
    if (local) { setRetroA(local); setErrorA(null); return; }

    let cancelled = false;
    setLoadingA(true);
    fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskAId)}/retrospective`))
      .then((res) => {
        if (!res.ok) throw new Error("Failed to load retrospective");
        return res.json();
      })
      .then((data: TaskRetrospective) => {
        if (!cancelled) { setRetroA(data); setErrorA(null); }
      })
      .catch((err) => {
        if (!cancelled) setErrorA(err instanceof Error ? err.message : "Failed to load");
      })
      .finally(() => { if (!cancelled) setLoadingA(false); });
    return () => { cancelled = true; };
  }, [taskAId, retrospectives]);

  useEffect(() => {
    if (!taskBId) { setRetroB(null); return; }
    const local = retrospectives?.find((r) => r.task_id === taskBId);
    if (local) { setRetroB(local); setErrorB(null); return; }

    let cancelled = false;
    setLoadingB(true);
    fetch(projectUrl(`/api/tasks/${encodeURIComponent(taskBId)}/retrospective`))
      .then((res) => {
        if (!res.ok) throw new Error("Failed to load retrospective");
        return res.json();
      })
      .then((data: TaskRetrospective) => {
        if (!cancelled) { setRetroB(data); setErrorB(null); }
      })
      .catch((err) => {
        if (!cancelled) setErrorB(err instanceof Error ? err.message : "Failed to load");
      })
      .finally(() => { if (!cancelled) setLoadingB(false); });
    return () => { cancelled = true; };
  }, [taskBId, retrospectives]);

  function findTask(id: string): TaskManifest | undefined {
    return tasks.find((t) => t.task_id === id);
  }

  function renderSide(
    taskId: string,
    task: TaskManifest | undefined,
    retro: TaskRetrospective | null,
    isLoading: boolean,
    err: string | null,
  ) {
    if (!taskId) {
      return (
        <div className="flex items-center justify-center h-48 text-slate-600 font-mono text-xs">
          Select a task to compare
        </div>
      );
    }
    if (isLoading) {
      return (
        <div className="space-y-3 p-4" role="status" aria-label="Loading comparison data">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-4 w-36" />
          <Skeleton className="h-4 w-28" />
        </div>
      );
    }
    if (err) {
      return (
        <div className="p-4" role="alert">
          <p className="text-red-400 font-mono text-xs">{err}</p>
        </div>
      );
    }

    const totalFindings = retro
      ? Object.values(retro.findings_by_category).reduce((a, b) => a + b, 0)
      : 0;

    return (
      <div className="space-y-3 p-4 font-mono text-xs">
        <div className="flex justify-between">
          <span className="text-slate-500">Quality Score</span>
          <span className="text-[#BDF000]">
            {retro ? Math.round(retro.quality_score * 100) + "%" : "--"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Cost Score</span>
          <span className="text-[#B47AFF]">
            {retro ? Math.round(retro.cost_score * 100) + "%" : "--"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Findings</span>
          <span className="text-slate-300">{retro ? totalFindings : "--"}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Stage</span>
          <span style={{ color: getStageColor(task?.stage ?? "") }}>
            {task?.stage ?? "--"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Type</span>
          <span className="text-slate-300">
            {task?.classification?.type ?? "--"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Risk</span>
          <span style={{ color: getRiskColor(task?.classification?.risk_level ?? "") }}>
            {task?.classification?.risk_level ?? "--"}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-slate-500">Repair Cycles</span>
          <span className="text-slate-300">
            {retro ? retro.repair_cycle_count : "--"}
          </span>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
        {/* Task A */}
        <div className="space-y-3">
          <label htmlFor="compare-task-a" className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">
            Task A
          </label>
          <select
            id="compare-task-a"
            value={taskAId}
            onChange={(e) => setTaskAId(e.target.value)}
            aria-label="Select first task to compare"
            className="w-full bg-[#0F1114]/60 border border-[#BDF000]/20 text-slate-200 px-3 py-2 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded"
          >
            <option value="">Select a task</option>
            {taskOptions.map((id) => (
              <option key={id} value={id}>{id}</option>
            ))}
          </select>
          <div className="bg-[#0F1114]/40 border border-[#BDF000]/10 rounded-lg min-h-[200px]">
            {renderSide(taskAId, findTask(taskAId), retroA, loadingA, errorA)}
          </div>
        </div>

        {/* Task B */}
        <div className="space-y-3">
          <label htmlFor="compare-task-b" className="text-slate-500 font-mono text-[10px] uppercase tracking-wider">
            Task B
          </label>
          <select
            id="compare-task-b"
            value={taskBId}
            onChange={(e) => setTaskBId(e.target.value)}
            aria-label="Select second task to compare"
            className="w-full bg-[#0F1114]/60 border border-[#BDF000]/20 text-slate-200 px-3 py-2 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded"
          >
            <option value="">Select a task</option>
            {taskOptions.map((id) => (
              <option key={id} value={id}>{id}</option>
            ))}
          </select>
          <div className="bg-[#0F1114]/40 border border-[#BDF000]/10 rounded-lg min-h-[200px]">
            {renderSide(taskBId, findTask(taskBId), retroB, loadingB, errorB)}
          </div>
        </div>
      </div>

      {/* Empty state for compare */}
      {!taskAId && !taskBId && tasks.length === 0 && (
        <div className="flex flex-col items-center justify-center py-12" role="status">
          <AlertCircle className="w-8 h-8 text-slate-600 mb-3" aria-hidden="true" />
          <p className="text-slate-400 font-mono text-sm">No tasks available to compare</p>
          <p className="text-slate-600 font-mono text-xs mt-1">
            Tasks will appear here once created via the CLI.
          </p>
        </div>
      )}
    </div>
  );
}

// ---- Main Page ----

export default function TaskPipeline() {
  const { isGlobal } = useProject();
  const [search, setSearch] = useState("");

  const {
    data: tasks,
    loading,
    error,
    refetch,
  } = usePollingData<TaskManifest[]>("/api/tasks");

  const {
    data: retrospectives,
  } = usePollingData<TaskRetrospective[]>("/api/retrospectives", 10000);

  const handleSearch = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      setSearch(e.target.value);
    },
    [],
  );

  // Sort by created_at descending, then filter by search
  const sortedAndFiltered = (() => {
    if (!tasks) return [];
    const sorted = [...tasks].sort(
      (a, b) =>
        new Date(b.created_at).getTime() - new Date(a.created_at).getTime(),
    );
    if (!search.trim()) return sorted;
    const q = search.toLowerCase();
    return sorted.filter(
      (t) =>
        t.task_id.toLowerCase().includes(q) ||
        t.title.toLowerCase().includes(q),
    );
  })();

  // Determine UI state
  const isInitialLoading = loading && tasks === null;
  const isError = error !== null && tasks === null;
  const isStaleError = error !== null && tasks !== null;
  const isEmpty = !loading && !error && tasks !== null && tasks.length === 0;
  const isSearchEmpty = sortedAndFiltered.length === 0 && !isEmpty && !isInitialLoading && !isError;

  // AC-3: Summary metrics
  const totalTasks = tasks ? tasks.length : 0;
  const activeTasks = tasks
    ? tasks.filter((t) => t.stage !== "DONE" && !t.stage.includes("FAIL") && !t.stage.includes("BLOCKED")).length
    : 0;
  const completedTasks = tasks
    ? tasks.filter((t) => t.stage === "DONE").length
    : 0;
  const failedTasks = tasks
    ? tasks.filter((t) => t.stage.includes("FAIL") || t.stage.includes("BLOCKED")).length
    : 0;

  const pipelineContent = (
    <>
      {/* AC-3: Summary row */}
      {tasks !== null && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-5" role="region" aria-label="Task summary metrics">
          <MetricCard
            label="Total Tasks"
            value={totalTasks}
            trend={null}
            icon={<ListChecks className="w-3.5 h-3.5 text-[#7A776E]" aria-hidden="true" />}
            delay={0}
          />
          <MetricCard
            label="Active"
            value={activeTasks}
            trend={null}
            icon={<Play className="w-3.5 h-3.5 text-[#FF9F43]" aria-hidden="true" />}
            delay={0.05}
          />
          <MetricCard
            label="Completed"
            value={completedTasks}
            trend={null}
            icon={<CheckCircle2 className="w-3.5 h-3.5 text-[#2DD4A8]" aria-hidden="true" />}
            delay={0.1}
          />
          <MetricCard
            label="Failed"
            value={failedTasks}
            trend={null}
            icon={<XCircle className="w-3.5 h-3.5 text-[#FF3B3B]" aria-hidden="true" />}
            delay={0.15}
          />
        </div>
      )}

      {/* Search */}
      <div className="flex gap-4 mb-6 relative z-10">
        <div className="relative flex-1 max-w-sm">
          <Search
            className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[#BDF000]/50"
            aria-hidden="true"
          />
          <input
            type="text"
            placeholder="Search task ID or title..."
            value={search}
            onChange={handleSearch}
            aria-label="Search tasks by ID or title"
            className="w-full bg-[#0F1114]/60 border border-[#BDF000]/20 text-slate-200 placeholder-slate-600 px-10 py-2 font-mono text-xs focus:outline-none focus:border-[#BDF000] transition-colors rounded"
          />
        </div>
      </div>

      {/* Stale error banner */}
      {isStaleError && <StaleErrorBanner message={error} onRetry={refetch} />}

      {/* Loading skeleton */}
      {isInitialLoading && <SkeletonTable rows={6} />}

      {/* Error state */}
      {isError && <ErrorCard message={error} onRetry={refetch} />}

      {/* Empty state */}
      {isEmpty && <EmptyState />}

      {/* Search yielded no results */}
      {isSearchEmpty && (
        <div className="flex flex-col items-center justify-center py-16" role="status">
          <Search className="w-8 h-8 text-slate-600 mb-3" aria-hidden="true" />
          <p className="text-slate-400 font-mono text-sm">No tasks found</p>
          <p className="text-slate-600 font-mono text-xs mt-1">
            No tasks match the current search filter.
          </p>
        </div>
      )}

      {/* Data table */}
      {sortedAndFiltered.length > 0 && (
        <div className="flex-1 overflow-auto bg-[#0F1114]/40 border border-[#BDF000]/10 backdrop-blur-sm rounded relative">
          <table className="w-full text-left border-collapse">
            <thead>
              <tr className="border-b border-white/5 bg-white/5 font-mono text-xs text-slate-500">
                {isGlobal && (
                  <th className="p-4 font-normal" scope="col">
                    <span className="inline-flex items-center gap-1">PROJECT <ChevronUp className="w-2.5 h-2.5 text-slate-600" aria-hidden="true" /></span>
                  </th>
                )}
                <th className="p-4 font-normal" scope="col">
                  <span className="inline-flex items-center gap-1">TASK ID <ChevronUp className="w-2.5 h-2.5 text-slate-600" aria-hidden="true" /></span>
                </th>
                <th className="p-4 font-normal" scope="col">
                  <span className="inline-flex items-center gap-1">TITLE <ChevronUp className="w-2.5 h-2.5 text-slate-600" aria-hidden="true" /></span>
                </th>
                <th className="p-4 font-normal" scope="col">
                  <span className="inline-flex items-center gap-1">STAGE <ChevronUp className="w-2.5 h-2.5 text-slate-600" aria-hidden="true" /></span>
                </th>
                <th className="p-4 font-normal" scope="col">
                  <span className="inline-flex items-center gap-1">TYPE <ChevronUp className="w-2.5 h-2.5 text-slate-600" aria-hidden="true" /></span>
                </th>
                <th className="p-4 font-normal" scope="col">
                  <span className="inline-flex items-center gap-1">RISK <ChevronUp className="w-2.5 h-2.5 text-slate-600" aria-hidden="true" /></span>
                </th>
                <th className="p-4 font-normal" scope="col">
                  <span className="inline-flex items-center gap-1">QUALITY SCORE <ChevronUp className="w-2.5 h-2.5 text-slate-600" aria-hidden="true" /></span>
                </th>
                <th className="p-4 font-normal" scope="col">TIMELINE</th>
                <th className="p-4 font-normal" scope="col">STATUS</th>
              </tr>
            </thead>
            <tbody className="font-mono text-sm">
              {sortedAndFiltered.map((task, idx) => (
                <TaskRow
                  key={task.task_id}
                  task={task}
                  qualityDisplay={formatQualityScore(retrospectives, task.task_id)}
                  isGlobal={isGlobal}
                  index={idx}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );

  return (
    <div className="p-6 sm:p-8 h-full flex flex-col">
      {/* Header */}
      <header className="mb-8">
        <h1 className="text-3xl font-mono font-light tracking-[0.2em] text-[#B47AFF]">
          TASK PIPELINE
        </h1>
        <p className="text-slate-500 font-mono text-xs mt-2">
          // LIFECYCLE TRACKING & EXECUTION STATUS
        </p>
      </header>

      {/* AC-15: Tabs for Pipeline and Compare */}
      <Tabs defaultValue="pipeline" className="flex-1 flex flex-col">
        <TabsList className="bg-[#0F1114]/60 border border-[#BDF000]/10 mb-6">
          <TabsTrigger
            value="pipeline"
            className="data-[state=active]:bg-[#BDF000]/10 data-[state=active]:text-[#BDF000] text-slate-500 font-mono text-xs tracking-wider"
          >
            PIPELINE
          </TabsTrigger>
          <TabsTrigger
            value="compare"
            className="data-[state=active]:bg-[#B47AFF]/10 data-[state=active]:text-[#B47AFF] text-slate-500 font-mono text-xs tracking-wider"
          >
            COMPARE
          </TabsTrigger>
        </TabsList>

        <TabsContent value="pipeline" className="flex-1 flex flex-col">
          {pipelineContent}
        </TabsContent>

        <TabsContent value="compare">
          {isInitialLoading && (
            <div className="space-y-3" role="status" aria-label="Loading tasks for comparison">
              <Skeleton className="h-8 w-64" />
              <Skeleton className="h-8 w-64" />
            </div>
          )}
          {isError && <ErrorCard message={error} onRetry={refetch} />}
          {tasks !== null && (
            <CompareTab tasks={tasks} retrospectives={retrospectives} />
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}
