import { useParams, Link } from "react-router";
import { useState, useMemo } from "react";
import { motion } from "motion/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowLeft, FileText, GitBranch, Shield, DollarSign, Activity,
  Clock, Zap, FileX, ChevronDown, ChevronRight, CheckCircle2,
  XCircle, AlertTriangle, Network, BookOpen,
} from "lucide-react";
import { usePollingData } from "@/data/hooks";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricCard } from "@/components/MetricCard";
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from "@/components/ui/collapsible";
import type {
  TaskManifest, TaskRetrospective, ExecutionGraph, ExecutionSegment,
  AuditReport, AuditFinding, TokenUsage,
  TaskEventsResponse, TaskReceiptsResponse, TaskEvidenceResponse,
  TaskCompletion, TaskPostmortem, RouterDecision, RouterDecisionsResponse,
  MarkdownContent,
} from "@/data/types";

// ---- Constants (shared with TaskPipeline) ----

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

const IN_PROGRESS_COLOR = "#FF9F43";

function getStageColor(stage: string): string {
  if (stage === "DONE") return STAGE_COLORS.DONE;
  if (stage.includes("FAIL")) return STAGE_COLORS.FAILED;
  if (stage.includes("BLOCKED")) return STAGE_COLORS.BLOCKED;
  return IN_PROGRESS_COLOR;
}

function getRiskColor(risk: string): string {
  return RISK_COLORS[risk] ?? "#999";
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

function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return String(n);
}

function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return iso.slice(11, 19);
  }
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString("en-US", {
      month: "short", day: "numeric", year: "numeric",
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
  } catch {
    return iso;
  }
}

// ---- Reusable sub-components ----

function SectionCard({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <motion.div
      className="border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-5"
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.28, ease: "easeOut" }}
    >
      <div className="flex items-center gap-1.5 mb-4">
        {icon}
        <span className="text-[10px] text-[#7A776E] tracking-[0.12em] uppercase font-medium">
          {title}
        </span>
      </div>
      {children}
    </motion.div>
  );
}

function NotAvailable({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 py-6 justify-center text-[#5A574E]">
      <FileX className="w-4 h-4" />
      <span className="text-xs font-mono">{label} not available yet</span>
    </div>
  );
}

function SectionSkeleton() {
  return (
    <div className="space-y-3" role="status" aria-label="Loading">
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-3/4" />
      <Skeleton className="h-4 w-1/2" />
    </div>
  );
}

function MarkdownBlock({ content }: { content: string }) {
  return (
    <div className="prose prose-invert prose-sm max-w-none text-slate-300 [&_pre]:bg-black/30 [&_pre]:p-3 [&_pre]:rounded [&_code]:text-[#BDF000] [&_h1]:text-base [&_h2]:text-sm [&_h3]:text-xs [&_table]:text-xs [&_a]:text-[#BDF000] [&_li]:text-xs [&_p]:text-xs">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
        {content}
      </ReactMarkdown>
    </div>
  );
}

function Badge({ label, color }: { label: string; color: string }) {
  return (
    <span
      className="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider shrink-0"
      style={{ color, borderColor: color + "4D", backgroundColor: color + "1A", border: `1px solid ${color}4D` }}
    >
      {label}
    </span>
  );
}

function KvRow({ label, value, mono }: { label: string; value: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline gap-3 py-1.5 border-b border-white/5 last:border-0">
      <span className="text-[10px] text-[#7A776E] uppercase tracking-wider w-32 shrink-0">{label}</span>
      <span className={`text-xs text-slate-300 ${mono ? "font-mono" : ""}`}>{value}</span>
    </div>
  );
}

// ---- Tab content components ----

function OverviewTab({ taskId }: { taskId: string }) {
  const { data: rawInput, loading: riLoading } = usePollingData<MarkdownContent>(`/api/tasks/${taskId}/raw-input`, 30000);
  const { data: retro, loading: retroLoading } = usePollingData<TaskRetrospective>(`/api/tasks/${taskId}/retrospective`, 30000);

  return (
    <div className="space-y-5">
      {/* Raw Input */}
      <SectionCard title="Original Prompt" icon={<FileText className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {riLoading && <SectionSkeleton />}
        {!riLoading && rawInput?.content ? (
          <MarkdownBlock content={rawInput.content} />
        ) : !riLoading ? (
          <NotAvailable label="Raw input" />
        ) : null}
      </SectionCard>

      {/* Retrospective Scores */}
      <SectionCard title="Quality Scores" icon={<Activity className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {retroLoading && <SectionSkeleton />}
        {!retroLoading && retro ? (
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-4">
              <MetricCard label="Quality" value={`${Math.round(retro.quality_score * 100)}%`} icon={<CheckCircle2 className="w-3 h-3 text-[#BDF000]" />} />
              <MetricCard label="Cost" value={`${Math.round(retro.cost_score * 100)}%`} icon={<DollarSign className="w-3 h-3 text-[#2DD4A8]" />} />
              <MetricCard label="Efficiency" value={`${Math.round(retro.efficiency_score * 100)}%`} icon={<Zap className="w-3 h-3 text-[#FF9F43]" />} />
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6">
              <KvRow label="Outcome" value={retro.task_outcome || "UNKNOWN"} mono />
              <KvRow label="Task Type" value={retro.task_type || "unknown"} mono />
              <KvRow label="Risk Level" value={retro.task_risk_level || "unknown"} mono />
              <KvRow
                label="Domains"
                value={Array.isArray(retro.task_domains) ? retro.task_domains.join(", ") : String(retro.task_domains ?? "")}
                mono
              />
            </div>
          </div>
        ) : !retroLoading ? (
          <NotAvailable label="Retrospective scores" />
        ) : null}
      </SectionCard>
    </div>
  );
}

function DiscoveryDesignTab({ taskId }: { taskId: string }) {
  const { data: discovery, loading: dLoading } = usePollingData<MarkdownContent>(`/api/tasks/${taskId}/discovery-notes`, 30000);
  const { data: design, loading: ddLoading } = usePollingData<MarkdownContent>(`/api/tasks/${taskId}/design-decisions`, 30000);

  return (
    <div className="space-y-5">
      <SectionCard title="Discovery Notes" icon={<FileText className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {dLoading && <SectionSkeleton />}
        {!dLoading && discovery?.content ? <MarkdownBlock content={discovery.content} /> : !dLoading ? <NotAvailable label="Discovery notes" /> : null}
      </SectionCard>
      <SectionCard title="Design Decisions" icon={<FileText className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {ddLoading && <SectionSkeleton />}
        {!ddLoading && design?.content ? <MarkdownBlock content={design.content} /> : !ddLoading ? <NotAvailable label="Design decisions" /> : null}
      </SectionCard>
    </div>
  );
}

function SpecPlanTab({ taskId }: { taskId: string }) {
  const { data: spec, loading: sLoading } = usePollingData<MarkdownContent>(`/api/tasks/${taskId}/spec`, 30000);
  const { data: plan, loading: pLoading } = usePollingData<MarkdownContent>(`/api/tasks/${taskId}/plan`, 30000);
  const { data: graph, loading: gLoading } = usePollingData<ExecutionGraph>(`/api/tasks/${taskId}/execution-graph`, 30000);

  return (
    <div className="space-y-5">
      <SectionCard title="Spec (Acceptance Criteria)" icon={<FileText className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {sLoading && <SectionSkeleton />}
        {!sLoading && spec?.content ? <MarkdownBlock content={spec.content} /> : !sLoading ? <NotAvailable label="Spec" /> : null}
      </SectionCard>
      <SectionCard title="Implementation Plan" icon={<FileText className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {pLoading && <SectionSkeleton />}
        {!pLoading && plan?.content ? <MarkdownBlock content={plan.content} /> : !pLoading ? <NotAvailable label="Plan" /> : null}
      </SectionCard>
      <SectionCard title="Execution Graph" icon={<GitBranch className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {gLoading && <SectionSkeleton />}
        {!gLoading && graph?.segments && graph.segments.length > 0 ? (
          <div className="space-y-3">
            {graph.segments.map((seg: ExecutionSegment) => {
              const exColor = getExecutorColor(seg.executor);
              return (
                <div
                  key={seg.id}
                  className="border rounded-xl p-4 space-y-2"
                  style={{ borderColor: exColor + "3D", backgroundColor: exColor + "08" }}
                >
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-mono text-xs font-bold" style={{ color: exColor }}>{seg.id}</span>
                    <Badge label={seg.executor} color={exColor} />
                    {seg.parallelizable && <Badge label="parallel" color="#7A776E" />}
                  </div>
                  <p className="text-xs text-slate-400">{seg.description}</p>
                  {seg.depends_on.length > 0 && (
                    <div className="text-[10px] text-[#7A776E]">
                      Depends on: {seg.depends_on.map((d) => <span key={d} className="font-mono text-slate-400 mr-1">{d}</span>)}
                    </div>
                  )}
                  <div className="text-[10px] text-[#7A776E]">
                    Criteria: {seg.criteria_ids.map((c) => <span key={c} className="font-mono text-[#BDF000] mr-1">AC-{c}</span>)}
                  </div>
                  {seg.files_expected.length > 0 && (
                    <div className="text-[10px] text-[#7A776E]">
                      Files: {seg.files_expected.map((f) => <span key={f} className="font-mono text-slate-500 mr-1 block">{f}</span>)}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : !gLoading ? <NotAvailable label="Execution graph" /> : null}
      </SectionCard>
    </div>
  );
}

function ExecutionTab({ taskId }: { taskId: string }) {
  const { data: logData, loading: lLoading } = usePollingData<{ lines: string[] }>(`/api/tasks/${taskId}/execution-log`, 10000);
  const { data: eventsData, loading: eLoading } = usePollingData<TaskEventsResponse>(`/api/tasks/${taskId}/events`, 30000);
  const { data: evidenceData, loading: evLoading } = usePollingData<TaskEvidenceResponse>(`/api/tasks/${taskId}/evidence`, 30000);

  return (
    <div className="space-y-5">
      {/* Execution Log */}
      <SectionCard title="Execution Log" icon={<Clock className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {lLoading && <SectionSkeleton />}
        {!lLoading && logData?.lines && logData.lines.length > 0 ? (
          <div className="max-h-96 overflow-y-auto bg-black/30 rounded-lg p-3">
            {logData.lines.filter((l) => l.trim()).map((line, i) => {
              const tsMatch = line.match(/^(\d{4}-\d{2}-\d{2}T[\d:.]+Z?)\s/);
              return (
                <div key={i} className="font-mono text-[11px] leading-relaxed py-0.5 border-b border-white/3 last:border-0">
                  {tsMatch ? (
                    <>
                      <span className="text-[#B47AFF]">{tsMatch[1]}</span>
                      <span className="text-slate-400">{line.slice(tsMatch[1].length)}</span>
                    </>
                  ) : (
                    <span className="text-slate-400">{line}</span>
                  )}
                </div>
              );
            })}
          </div>
        ) : !lLoading ? <NotAvailable label="Execution log" /> : null}
      </SectionCard>

      {/* Events */}
      <SectionCard title="Event Stream" icon={<Activity className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {eLoading && <SectionSkeleton />}
        {!eLoading && eventsData?.events && eventsData.events.length > 0 ? (
          <div className="max-h-80 overflow-y-auto bg-black/30 rounded-lg p-3 space-y-0.5">
            {eventsData.events.map((evt, i) => (
              <div key={i} className="flex items-center gap-2 py-1 border-b border-white/3 last:border-0 text-[11px] font-mono flex-wrap">
                <span className="text-[#B47AFF] shrink-0 w-[52px]">{formatTime(evt.ts)}</span>
                <Badge label={String(evt.event)} color="#00E5FF" />
                {Object.entries(evt).filter(([k]) => !["ts", "event"].includes(k)).map(([k, v]) => (
                  <span key={k} className="text-slate-500">
                    <span className="text-[#7A776E]">{k}=</span>
                    <span className="text-slate-300">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
                  </span>
                ))}
              </div>
            ))}
          </div>
        ) : !eLoading ? <NotAvailable label="Events" /> : null}
      </SectionCard>

      {/* Evidence */}
      <SectionCard title="Segment Evidence" icon={<FileText className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {evLoading && <SectionSkeleton />}
        {!evLoading && evidenceData?.files && evidenceData.files.length > 0 ? (
          <div className="space-y-2">
            {evidenceData.files.map((file) => (
              <EvidenceCollapsible key={file.name} name={file.name} content={file.content} />
            ))}
          </div>
        ) : !evLoading ? <NotAvailable label="Evidence files" /> : null}
      </SectionCard>
    </div>
  );
}

function EvidenceCollapsible({ name, content }: { name: string; content: string }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger asChild>
        <button className="flex items-center gap-2 text-slate-400 hover:text-slate-200 font-mono text-xs transition-colors w-full text-left py-1">
          {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
          <FileText className="w-3 h-3" />
          {name}
        </button>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="mt-1 bg-black/30 rounded-lg p-3 max-h-64 overflow-auto">
          <MarkdownBlock content={content} />
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

function AuditQualityTab({ taskId }: { taskId: string }) {
  const { data: retro, loading: rLoading } = usePollingData<TaskRetrospective>(`/api/tasks/${taskId}/retrospective`, 30000);
  const { data: reports, loading: aLoading } = usePollingData<AuditReport[]>(`/api/tasks/${taskId}/audit-reports`, 30000);
  const { data: receiptsData, loading: rcLoading } = usePollingData<TaskReceiptsResponse>(`/api/tasks/${taskId}/receipts`, 30000);
  const { data: completion, loading: cLoading } = usePollingData<TaskCompletion>(`/api/tasks/${taskId}/completion`, 30000);

  const allFindings = useMemo(() => {
    if (!reports) return [];
    return reports.flatMap((r) => r.findings ?? []);
  }, [reports]);

  const blockingCount = useMemo(() => allFindings.filter((f) => f.blocking).length, [allFindings]);
  const nonBlockingCount = allFindings.length - blockingCount;

  return (
    <div className="space-y-5">
      {/* Audit Summary Bar */}
      <SectionCard title="Audit Summary" icon={<Shield className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {rLoading && <SectionSkeleton />}
        {!rLoading && retro ? (
          <div className="space-y-4">
            {/* Per-auditor finding counts */}
            <div className="space-y-2">
              <span className="text-[10px] text-[#7A776E] uppercase tracking-wider">Findings by Auditor</span>
              {Object.entries(retro.findings_by_auditor ?? {}).map(([auditor, count]) => {
                const streak = retro.auditor_zero_finding_streaks?.[auditor];
                return (
                  <div key={auditor} className="flex items-center gap-3">
                    <span className="text-xs text-slate-400 font-mono w-48 truncate" title={auditor}>{auditor}</span>
                    <div className="flex-1 h-2 bg-white/5 rounded-full overflow-hidden">
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: `${Math.min(100, (count as number) * 10)}%`,
                          backgroundColor: (count as number) === 0 ? "#2DD4A8" : (count as number) > 5 ? "#FF3B3B" : "#FF9F43",
                        }}
                      />
                    </div>
                    <span className="text-xs font-mono text-slate-300 w-8 text-right">{count as number}</span>
                    {streak !== undefined && streak !== null && (streak as number) > 0 && (
                      <span className="text-[10px] text-[#2DD4A8] font-mono" title="Consecutive clean audits">
                        {streak as number} clean
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
            {/* Blocking vs non-blocking */}
            <div className="flex gap-4">
              <div className="flex items-center gap-2">
                <XCircle className="w-3.5 h-3.5 text-[#FF3B3B]" />
                <span className="text-xs text-slate-400">Blocking: <span className="text-slate-200 font-mono">{blockingCount}</span></span>
              </div>
              <div className="flex items-center gap-2">
                <AlertTriangle className="w-3.5 h-3.5 text-[#FF9F43]" />
                <span className="text-xs text-slate-400">Non-blocking: <span className="text-slate-200 font-mono">{nonBlockingCount}</span></span>
              </div>
            </div>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6">
              <KvRow label="Spec Reviews" value={retro.spec_review_iterations ?? 0} mono />
              <KvRow label="Repair Cycles" value={retro.repair_cycle_count ?? 0} mono />
            </div>
          </div>
        ) : !rLoading ? <NotAvailable label="Audit summary" /> : null}
      </SectionCard>

      <SectionCard title="Findings by Category" icon={<AlertTriangle className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {rLoading && <SectionSkeleton />}
        {!rLoading && retro && Object.keys(retro.findings_by_category ?? {}).length > 0 ? (
          <div className="space-y-2">
            {Object.entries(retro.findings_by_category ?? {})
              .sort(([, a], [, b]) => (b as number) - (a as number))
              .map(([category, count]) => (
                <div key={category} className="flex items-center gap-3">
                  <span className="text-xs text-slate-400 font-mono w-32 truncate" title={category}>{category}</span>
                  <div className="flex-1 h-2 bg-white/5 rounded-full overflow-hidden">
                    <div
                      className="h-full rounded-full bg-[#B47AFF]"
                      style={{ width: `${Math.min(100, (count as number) * 10)}%` }}
                    />
                  </div>
                  <span className="text-xs font-mono text-slate-300 w-8 text-right">{count as number}</span>
                </div>
              ))}
          </div>
        ) : !rLoading ? <NotAvailable label="Category findings" /> : null}
      </SectionCard>

      <SectionCard title="Repair Execution" icon={<Zap className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {rLoading && <SectionSkeleton />}
        {!rLoading && retro ? (
          <div className="space-y-4">
            {Object.keys(retro.executor_repair_frequency ?? {}).length > 0 ? (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">Executor Repair Frequency</span>
                <div className="space-y-2">
                  {Object.entries(retro.executor_repair_frequency ?? {})
                    .sort(([, a], [, b]) => (b as number) - (a as number))
                    .map(([executor, count]) => {
                      const cleanStreak = retro.executor_zero_repair_streak?.[executor];
                      return (
                        <div key={executor} className="flex items-center gap-3">
                          <span className="text-xs text-slate-400 font-mono w-48 truncate" title={executor}>{executor}</span>
                          <div className="flex-1 h-2 bg-white/5 rounded-full overflow-hidden">
                            <div
                              className="h-full rounded-full"
                              style={{
                                width: `${Math.min(100, (count as number) * 10)}%`,
                                backgroundColor: (count as number) > 5 ? "#FF3B3B" : "#00E5FF",
                              }}
                            />
                          </div>
                          <span className="text-xs font-mono text-slate-300 w-8 text-right">{count as number}</span>
                          {cleanStreak !== undefined && cleanStreak !== null && (cleanStreak as number) > 0 && (
                            <span className="text-[10px] text-[#2DD4A8] font-mono" title="Consecutive tasks without repairs">
                              {cleanStreak as number} clean
                            </span>
                          )}
                        </div>
                      );
                    })}
                </div>
              </div>
            ) : (
              <NotAvailable label="Executor repair frequency" />
            )}
          </div>
        ) : !rLoading ? <NotAvailable label="Repair execution" /> : null}
      </SectionCard>

      {/* Audit Finding Cards */}
      <SectionCard title="Audit Findings" icon={<Shield className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {aLoading && <SectionSkeleton />}
        {!aLoading && reports && reports.length > 0 ? (
          <div className="space-y-4">
            {reports.map((report) => (
              <div key={report.auditor_name + report.timestamp}>
                <h4 className="text-xs font-mono text-slate-400 mb-2 flex items-center gap-2">
                  <Shield className="w-3 h-3" />
                  {report.auditor_name}
                  <span className="text-[10px] text-[#7A776E]">({report.scope})</span>
                  <span className="text-[10px] text-[#7A776E]">{report.findings?.length ?? 0} findings</span>
                </h4>
                {(report.findings ?? []).length === 0 ? (
                  <p className="text-xs text-[#2DD4A8] font-mono pl-5">No findings</p>
                ) : (
                  <div className="space-y-2 pl-5">
                    {report.findings.map((finding: AuditFinding) => {
                      const sevColor = getSeverityColor(finding.severity);
                      return (
                        <div
                          key={finding.id}
                          className="border rounded-lg p-3 space-y-1.5"
                          style={{
                            borderColor: finding.blocking ? "#FF3B3B66" : sevColor + "3D",
                            backgroundColor: finding.blocking ? "#FF3B3B08" : sevColor + "06",
                          }}
                        >
                          <div className="flex items-center gap-2 flex-wrap">
                            <Badge label={finding.severity} color={sevColor} />
                            {finding.blocking && <Badge label="BLOCKING" color="#FF3B3B" />}
                            <span className="text-[10px] text-[#7A776E] font-mono">{finding.category}</span>
                          </div>
                          <p className="text-xs text-slate-200 font-medium">{finding.title}</p>
                          <p className="text-[11px] text-slate-400">{finding.description}</p>
                          {finding.location && (
                            <p className="text-[10px] text-[#7A776E] font-mono">{finding.location}</p>
                          )}
                          {finding.evidence && finding.evidence.length > 0 && (
                            <div className="text-[10px] text-slate-500 font-mono space-y-0.5 bg-black/20 rounded p-2">
                              {finding.evidence.map((e, i) => <div key={i}>{e}</div>)}
                            </div>
                          )}
                          {finding.recommendation && (
                            <p className="text-[10px] text-[#BDF000]">{finding.recommendation}</p>
                          )}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            ))}
          </div>
        ) : !aLoading ? <NotAvailable label="Audit reports" /> : null}
      </SectionCard>

      {/* Receipts */}
      <SectionCard title="Receipts" icon={<FileText className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {rcLoading && <SectionSkeleton />}
        {!rcLoading && receiptsData?.receipts && receiptsData.receipts.length > 0 ? (
          <div className="space-y-2">
            {receiptsData.receipts.map((receipt) => (
              <div key={receipt.filename} className="border border-white/6 rounded-lg p-3 bg-black/20">
                <span className="text-xs font-mono text-[#BDF000] block mb-1">{receipt.filename}</span>
                <div className="text-[11px] text-slate-400 font-mono space-y-0.5">
                  {Object.entries(receipt.data).filter(([k]) => k !== "receipt_type").slice(0, 6).map(([k, v]) => (
                    <div key={k}>
                      <span className="text-[#7A776E]">{k}: </span>
                      <span className="text-slate-300">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        ) : !rcLoading ? <NotAvailable label="Receipts" /> : null}
      </SectionCard>

      {/* Completion */}
      <SectionCard title="Completion" icon={<CheckCircle2 className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {cLoading && <SectionSkeleton />}
        {!cLoading && completion ? (
          <div className="space-y-2">
            <div className="flex items-center gap-3">
              {completion.audit_result === "pass" ? (
                <CheckCircle2 className="w-5 h-5 text-[#2DD4A8]" />
              ) : (
                <XCircle className="w-5 h-5 text-[#FF3B3B]" />
              )}
              <span className="text-sm font-mono" style={{ color: completion.audit_result === "pass" ? "#2DD4A8" : "#FF3B3B" }}>
                {completion.audit_result?.toUpperCase() ?? "UNKNOWN"}
              </span>
            </div>
            {completion.files_changed && (
              <KvRow label="Files Changed" value={Array.isArray(completion.files_changed) ? completion.files_changed.length : completion.files_changed} mono />
            )}
            {completion.tests_passed !== undefined && (
              <KvRow label="Tests Passed" value={completion.tests_passed} mono />
            )}
            {completion.blocking_findings !== undefined && (
              <KvRow label="Blocking" value={completion.blocking_findings} mono />
            )}
            {completion.segments_completed !== undefined && (
              <KvRow label="Segments" value={`${completion.segments_completed}/${completion.segments_total}`} mono />
            )}
          </div>
        ) : !cLoading ? <NotAvailable label="Completion data" /> : null}
      </SectionCard>
    </div>
  );
}

function CostTokensTab({ taskId }: { taskId: string }) {
  const { data: tokenUsage, loading: tLoading } = usePollingData<TokenUsage>(`/api/tasks/${taskId}/token-usage`, 30000);
  const { data: retro, loading: rLoading } = usePollingData<TaskRetrospective>(`/api/tasks/${taskId}/retrospective`, 30000);

  const MODEL_BADGE_COLORS: Record<string, string> = {
    opus: "#7C4DFF",
    sonnet: "#00E5FF",
    haiku: "#00BFA5",
    none: "#555",
  };

  /** Pricing per 1M tokens (USD) — matches Analytics.tsx DEFAULT_RATES */
  const MODEL_RATES: Record<string, number> = {
    haiku: 0.25,
    sonnet: 3.0,
    opus: 15.0,
  };

  const estimateCost = (tokens: number, model: string): number =>
    (tokens / 1_000_000) * (MODEL_RATES[model] ?? 0);

  const formatUsd = (usd: number): string =>
    usd < 0.01 && usd > 0 ? "<$0.01" : `$${usd.toFixed(2)}`;

  return (
    <div className="space-y-5">
      {/* Token Ledger */}
      <SectionCard title="Token Usage Ledger" icon={<DollarSign className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {tLoading && <SectionSkeleton />}
        {!tLoading && tokenUsage ? (
          <div className="space-y-4">
            {/* Totals */}
            <div className="grid grid-cols-4 gap-3">
              <MetricCard label="Total Tokens" value={formatTokens(tokenUsage.total ?? 0)} icon={<Activity className="w-3 h-3 text-slate-400" />} />
              <MetricCard label="Input" value={formatTokens(tokenUsage.total_input_tokens ?? 0)} icon={<Activity className="w-3 h-3 text-[#7C4DFF]" />} />
              <MetricCard label="Output" value={formatTokens(tokenUsage.total_output_tokens ?? 0)} icon={<Activity className="w-3 h-3 text-[#00E5FF]" />} />
              <MetricCard
                label="Est. Cost"
                value={formatUsd(
                  tokenUsage.by_model
                    ? Object.entries(tokenUsage.by_model).reduce(
                        (sum, [model, info]) => sum + estimateCost(info.tokens ?? 0, model),
                        0,
                      )
                    : 0,
                )}
                icon={<DollarSign className="w-3 h-3 text-[#BDF000]" />}
              />
            </div>

            {/* By Agent Table */}
            {tokenUsage.by_agent && Object.keys(tokenUsage.by_agent).length > 0 && (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">By Agent</span>
                <div className="overflow-x-auto">
                  <table className="w-full font-mono text-xs">
                    <thead>
                      <tr className="border-b border-white/10">
                        <th className="text-left text-slate-500 py-1.5 pr-3">Agent</th>
                        <th className="text-left text-slate-500 py-1.5 pr-3">Model</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Input</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Output</th>
                        <th className="text-right text-slate-500 py-1.5">Total</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(tokenUsage.by_agent)
                        .sort(([, a], [, b]) => b.tokens - a.tokens)
                        .map(([agent, info]) => (
                          <tr key={agent} className="border-b border-white/5">
                            <td className="text-slate-300 py-1.5 pr-3 max-w-[200px] truncate" title={agent}>{agent}</td>
                            <td className="py-1.5 pr-3">
                              <span className="text-[10px] font-mono font-medium" style={{ color: MODEL_BADGE_COLORS[info.model] ?? "#999" }}>
                                {info.model}
                              </span>
                            </td>
                            <td className="text-right text-[#7C4DFF] py-1.5 pr-3">{formatTokens(info.input_tokens)}</td>
                            <td className="text-right text-[#00E5FF] py-1.5 pr-3">{formatTokens(info.output_tokens)}</td>
                            <td className="text-right text-slate-400 py-1.5">{formatTokens(info.tokens)}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}

            {/* By Model Table */}
            {tokenUsage.by_model && Object.keys(tokenUsage.by_model).length > 0 && (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">By Model</span>
                <div className="overflow-x-auto">
                  <table className="w-full font-mono text-xs">
                    <thead>
                      <tr className="border-b border-white/10">
                        <th className="text-left text-slate-500 py-1.5 pr-3">Model</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Input</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Output</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Total</th>
                        <th className="text-right text-slate-500 py-1.5">Est. Cost</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(tokenUsage.by_model)
                        .sort(([, a], [, b]) => b.tokens - a.tokens)
                        .map(([model, info]) => (
                          <tr key={model} className="border-b border-white/5">
                            <td className="py-1.5 pr-3">
                              <span className="text-xs font-mono font-medium" style={{ color: MODEL_BADGE_COLORS[model] ?? "#999" }}>
                                {model}
                              </span>
                            </td>
                            <td className="text-right text-[#7C4DFF] py-1.5 pr-3">{formatTokens(info.input_tokens)}</td>
                            <td className="text-right text-[#00E5FF] py-1.5 pr-3">{formatTokens(info.output_tokens)}</td>
                            <td className="text-right text-slate-400 py-1.5 pr-3">{formatTokens(info.tokens)}</td>
                            <td className="text-right text-[#BDF000] py-1.5">{formatUsd(estimateCost(info.tokens ?? 0, model))}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        ) : !tLoading ? <NotAvailable label="Token usage" /> : null}
      </SectionCard>

      {/* Retro Token Breakdown */}
      <SectionCard title="Retrospective Token Summary" icon={<Activity className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {rLoading && <SectionSkeleton />}
        {!rLoading && retro ? (
          <div className="space-y-4">
            <div className="grid grid-cols-3 gap-3">
              <MetricCard label="Total Tokens" value={formatTokens(retro.total_token_usage ?? 0)} />
              <MetricCard label="Spawns" value={retro.subagent_spawn_count ?? 0} />
              <MetricCard label="Wasted Spawns" value={retro.wasted_spawns ?? 0} icon={<AlertTriangle className="w-3 h-3 text-[#FF3B3B]" />} />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <MetricCard label="Retro Input" value={formatTokens(retro.total_input_tokens ?? 0)} />
              <MetricCard label="Retro Output" value={formatTokens(retro.total_output_tokens ?? 0)} />
            </div>
            {retro.token_usage_by_agent && Object.keys(retro.token_usage_by_agent).length > 0 && (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">Tokens by Agent (Retro)</span>
                <div className="space-y-1">
                  {Object.entries(retro.token_usage_by_agent)
                    .sort(([, a], [, b]) => (b as number) - (a as number))
                    .map(([agent, tokens]) => (
                      <div key={agent} className="flex items-center gap-3">
                        <span className="text-xs text-slate-400 font-mono w-48 truncate" title={agent}>{agent}</span>
                        <div className="flex-1 h-2 bg-white/5 rounded-full overflow-hidden">
                          <div
                            className="h-full bg-[#B47AFF] rounded-full"
                            style={{ width: `${Math.min(100, ((tokens as number) / (retro.total_token_usage || 1)) * 100)}%` }}
                          />
                        </div>
                        <span className="text-xs font-mono text-slate-300 w-16 text-right">{formatTokens(tokens as number)}</span>
                      </div>
                  ))}
                </div>
              </div>
            )}
            {retro.input_tokens_by_agent && Object.keys(retro.input_tokens_by_agent).length > 0 && (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">Retrospective Agent IO</span>
                <div className="overflow-x-auto">
                  <table className="w-full font-mono text-xs">
                    <thead>
                      <tr className="border-b border-white/10">
                        <th className="text-left text-slate-500 py-1.5 pr-3">Agent</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Input</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Output</th>
                        <th className="text-right text-slate-500 py-1.5">Total</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.keys(retro.input_tokens_by_agent)
                        .sort((a, b) => (retro.token_usage_by_agent?.[b] ?? 0) - (retro.token_usage_by_agent?.[a] ?? 0))
                        .map((agent) => (
                          <tr key={agent} className="border-b border-white/5">
                            <td className="text-slate-300 py-1.5 pr-3 max-w-[200px] truncate" title={agent}>{agent}</td>
                            <td className="text-right text-[#7C4DFF] py-1.5 pr-3">{formatTokens(retro.input_tokens_by_agent?.[agent] ?? 0)}</td>
                            <td className="text-right text-[#00E5FF] py-1.5 pr-3">{formatTokens(retro.output_tokens_by_agent?.[agent] ?? 0)}</td>
                            <td className="text-right text-slate-400 py-1.5">{formatTokens(retro.token_usage_by_agent?.[agent] ?? 0)}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
            {retro.token_usage_by_model && Object.keys(retro.token_usage_by_model).length > 0 && (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">Retrospective Tokens by Model</span>
                <div className="overflow-x-auto">
                  <table className="w-full font-mono text-xs">
                    <thead>
                      <tr className="border-b border-white/10">
                        <th className="text-left text-slate-500 py-1.5 pr-3">Model</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Input</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Output</th>
                        <th className="text-right text-slate-500 py-1.5 pr-3">Total</th>
                        <th className="text-right text-slate-500 py-1.5">Est. Cost</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(retro.token_usage_by_model)
                        .sort(([, a], [, b]) => (b.tokens ?? 0) - (a.tokens ?? 0))
                        .map(([model, info]) => (
                          <tr key={model} className="border-b border-white/5">
                            <td className="py-1.5 pr-3">
                              <span className="text-xs font-mono font-medium" style={{ color: MODEL_BADGE_COLORS[model] ?? "#999" }}>
                                {model}
                              </span>
                            </td>
                            <td className="text-right text-[#7C4DFF] py-1.5 pr-3">{formatTokens(info.input_tokens ?? 0)}</td>
                            <td className="text-right text-[#00E5FF] py-1.5 pr-3">{formatTokens(info.output_tokens ?? 0)}</td>
                            <td className="text-right text-slate-400 py-1.5 pr-3">{formatTokens(info.tokens ?? 0)}</td>
                            <td className="text-right text-[#BDF000] py-1.5">{formatUsd(estimateCost(info.tokens ?? 0, model))}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              </div>
            )}
          </div>
        ) : !rLoading ? <NotAvailable label="Retrospective token data" /> : null}
      </SectionCard>
    </div>
  );
}

interface MergedRouterDecision {
  role: string;
  task_type?: string;
  model?: string;
  model_source?: string;
  mode?: string;
  agent_name?: string;
  composite_score?: number;
  route_source?: string;
  auditors?: Array<{ name: string; action: string; model?: string }>;
}

function mergeRouterDecisions(decisions: RouterDecision[]): MergedRouterDecision[] {
  const byRole = new Map<string, MergedRouterDecision>();

  for (const dec of decisions) {
    const role = (dec.role as string) ?? "unknown";

    // Handle router_audit_plan separately — it contains multiple auditors
    if (dec.event === "router_audit_plan") {
      const auditors = (dec.auditors ?? dec.auditor_count) as Array<{ name: string; action: string; model?: string }> | undefined;
      if (Array.isArray(auditors)) {
        for (const aud of auditors) {
          const key = aud.name;
          const existing = byRole.get(key) ?? { role: key, task_type: dec.task_type as string };
          existing.model = aud.model;
          existing.mode = aud.action;
          byRole.set(key, existing);
        }
      }
      continue;
    }

    const existing = byRole.get(role) ?? { role, task_type: dec.task_type as string };

    if (dec.event === "router_model_decision") {
      existing.model = dec.model as string;
      existing.model_source = dec.source as string;
    } else if (dec.event === "router_route_decision") {
      existing.mode = dec.mode as string;
      existing.agent_name = dec.agent_name as string;
      existing.composite_score = dec.composite_score as number;
      existing.route_source = dec.source as string;
    }

    byRole.set(role, existing);
  }

  return Array.from(byRole.values());
}

function RouterDecisionsTab({ taskId }: { taskId: string }) {
  const { data, loading } = usePollingData<RouterDecisionsResponse>(`/api/tasks/${taskId}/router-decisions`, 30000);
  const { data: retro, loading: retroLoading } = usePollingData<TaskRetrospective>(`/api/tasks/${taskId}/retrospective`, 30000);

  const MODE_COLORS: Record<string, string> = {
    replace: "#BDF000",
    shadow: "#B47AFF",
    alongside: "#FF9F43",
    spawn: "#2DD4A8",
    skip: "#7A776E",
    default: "#7A776E",
  };

  const MODEL_COLORS: Record<string, string> = {
    opus: "#7C4DFF",
    sonnet: "#00E5FF",
    haiku: "#00BFA5",
  };

  const merged = useMemo(() => {
    if (!data?.decisions) return [];
    return mergeRouterDecisions(data.decisions as RouterDecision[]);
  }, [data]);

  return (
    <div className="space-y-5">
      <SectionCard title="Router Decisions" icon={<Network className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {loading && <SectionSkeleton />}
        {!loading && merged.length > 0 ? (
          <div className="space-y-2">
            {merged.map((dec) => (
              <div key={dec.role} className="border border-white/6 rounded-lg p-3 bg-black/20 space-y-1.5">
                <div className="flex items-center gap-2 flex-wrap">
                  <span className="text-xs font-mono text-slate-200 font-medium">{dec.role}</span>
                  {dec.mode && <Badge label={dec.mode} color={MODE_COLORS[dec.mode] ?? "#7A776E"} />}
                  {dec.model && (
                    <Badge label={dec.model} color={MODEL_COLORS[dec.model] ?? "#999"} />
                  )}
                </div>
                <div className="text-[11px] font-mono space-y-0.5">
                  {dec.model_source && (
                    <div>
                      <span className="text-[#7A776E]">model source: </span>
                      <span className="text-slate-400">{dec.model_source}</span>
                    </div>
                  )}
                  {dec.agent_name && (
                    <div>
                      <span className="text-[#7A776E]">agent: </span>
                      <span className="text-[#BDF000]">{dec.agent_name}</span>
                    </div>
                  )}
                  {dec.composite_score !== undefined && dec.composite_score > 0 && (
                    <div>
                      <span className="text-[#7A776E]">score: </span>
                      <span className="text-slate-300">{dec.composite_score.toFixed(4)}</span>
                    </div>
                  )}
                  {dec.route_source && (
                    <div>
                      <span className="text-[#7A776E]">route: </span>
                      <span className="text-slate-400">{dec.route_source}</span>
                    </div>
                  )}
                  {dec.task_type && (
                    <div>
                      <span className="text-[#7A776E]">task_type: </span>
                      <span className="text-slate-400">{dec.task_type}</span>
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        ) : !loading ? <NotAvailable label="Router decisions" /> : null}
      </SectionCard>

      <SectionCard title="Recorded Routing Metadata" icon={<Network className="w-3.5 h-3.5 text-[#7A776E]" />}>
        {retroLoading && <SectionSkeleton />}
        {!retroLoading && retro ? (
          <div className="space-y-4">
            {Object.keys(retro.model_used_by_agent ?? {}).length > 0 && (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">Model Used by Agent</span>
                <div className="space-y-1">
                  {Object.entries(retro.model_used_by_agent ?? {}).map(([agent, model]) => (
                    <KvRow key={agent} label={agent} value={String(model)} mono />
                  ))}
                </div>
              </div>
            )}
            {Object.keys(retro.agent_source ?? {}).length > 0 && (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">Agent Source</span>
                <div className="space-y-1">
                  {Object.entries(retro.agent_source ?? {}).map(([agent, source]) => (
                    <KvRow key={agent} label={agent} value={String(source)} mono />
                  ))}
                </div>
              </div>
            )}
            {retro.alongside_overlap && Object.keys(retro.alongside_overlap).length > 0 && (
              <div>
                <span className="text-[10px] text-[#7A776E] uppercase tracking-wider block mb-2">Alongside Overlap</span>
                <div className="text-[11px] font-mono text-slate-300 bg-black/20 rounded-lg p-3 overflow-x-auto">
                  <pre>{JSON.stringify(retro.alongside_overlap, null, 2)}</pre>
                </div>
              </div>
            )}
            {!Object.keys(retro.model_used_by_agent ?? {}).length && !Object.keys(retro.agent_source ?? {}).length && !(retro.alongside_overlap && Object.keys(retro.alongside_overlap).length > 0) && (
              <NotAvailable label="Recorded routing metadata" />
            )}
          </div>
        ) : !retroLoading ? <NotAvailable label="Recorded routing metadata" /> : null}
      </SectionCard>
    </div>
  );
}

function PostmortemTab({ taskId }: { taskId: string }) {
  const { data, loading } = usePollingData<TaskPostmortem>(`/api/tasks/${taskId}/postmortem`, 30000);

  return (
    <div className="space-y-5">
      {loading && (
        <SectionCard title="Postmortem" icon={<BookOpen className="w-3.5 h-3.5 text-[#7A776E]" />}>
          <SectionSkeleton />
        </SectionCard>
      )}
      {!loading && data ? (
        <>
          {data.json && (
            <SectionCard title="Postmortem (JSON)" icon={<BookOpen className="w-3.5 h-3.5 text-[#7A776E]" />}>
              <div className="space-y-1">
                {Object.entries(data.json).map(([k, v]) => (
                  <KvRow key={k} label={k} value={typeof v === "object" ? JSON.stringify(v, null, 2) : String(v)} mono />
                ))}
              </div>
            </SectionCard>
          )}
          {data.markdown && (
            <SectionCard title="Postmortem" icon={<BookOpen className="w-3.5 h-3.5 text-[#7A776E]" />}>
              <MarkdownBlock content={data.markdown} />
            </SectionCard>
          )}
          {!data.json && !data.markdown && <NotAvailable label="Postmortem" />}
        </>
      ) : !loading ? (
        <SectionCard title="Postmortem" icon={<BookOpen className="w-3.5 h-3.5 text-[#7A776E]" />}>
          <NotAvailable label="Postmortem" />
        </SectionCard>
      ) : null}
    </div>
  );
}

// ---- Main Page Component ----

export default function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  const { data: manifest, loading: mLoading } = usePollingData<TaskManifest>(`/api/tasks/${taskId}/manifest`, 10000);

  if (!taskId) {
    return (
      <div className="flex items-center justify-center h-full">
        <p className="text-red-400 font-mono text-sm">Invalid task ID</p>
      </div>
    );
  }

  const stageColor = manifest ? getStageColor(manifest.stage) : "#999";
  const riskColor = manifest ? getRiskColor(manifest.classification?.risk_level) : "#999";

  return (
    <div className="space-y-6 pb-12">
      {/* Breadcrumb */}
      <nav className="flex items-center gap-2 text-xs font-mono">
        <Link to="/tasks" className="text-[#BDF000] hover:text-[#d4ff4d] transition-colors flex items-center gap-1">
          <ArrowLeft className="w-3 h-3" />
          Tasks
        </Link>
        <span className="text-[#5A574E]">&gt;</span>
        <span className="text-slate-400">{taskId}</span>
      </nav>

      {/* Header Banner */}
      <motion.div
        className="border border-white/6 bg-gradient-to-b from-[#222222] to-[#141414] rounded-2xl p-6"
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.28, ease: "easeOut" }}
      >
        {mLoading && <SectionSkeleton />}
        {!mLoading && manifest && (
          <div className="space-y-3">
            <div className="flex items-center gap-3 flex-wrap">
              <h1 className="text-lg font-bold text-[#F0F0E8] font-mono">{taskId}</h1>
              <Badge label={manifest.stage} color={stageColor} />
              {manifest.fast_track && <Badge label="FAST TRACK" color="#BDF000" />}
            </div>
            <p className="text-sm text-slate-300">{manifest.title}</p>
            <div className="flex items-center gap-4 flex-wrap">
              {manifest.classification && (
                <>
                  <Badge label={manifest.classification.type} color="#00E5FF" />
                  {manifest.classification.domains?.map((d) => (
                    <Badge key={d} label={d} color="#B47AFF" />
                  ))}
                  <Badge label={manifest.classification.risk_level} color={riskColor} />
                </>
              )}
            </div>
            <div className="flex items-center gap-6 text-[11px] text-[#7A776E] font-mono">
              <span>Created: {formatDate(manifest.created_at)}</span>
              {manifest.completed_at && <span>Completed: {formatDate(manifest.completed_at)}</span>}
              {manifest.snapshot && <span>Branch: {manifest.snapshot.branch}</span>}
            </div>
          </div>
        )}
        {!mLoading && !manifest && <NotAvailable label="Task manifest" />}
      </motion.div>

      {/* Tabbed Content */}
      <Tabs defaultValue="overview">
        <TabsList className="bg-[#1a1a1a] border border-white/6 rounded-xl overflow-x-auto">
          <TabsTrigger value="overview" className="text-xs font-mono">Overview</TabsTrigger>
          <TabsTrigger value="discovery" className="text-xs font-mono">Discovery & Design</TabsTrigger>
          <TabsTrigger value="spec-plan" className="text-xs font-mono">Spec & Plan</TabsTrigger>
          <TabsTrigger value="execution" className="text-xs font-mono">Execution</TabsTrigger>
          <TabsTrigger value="audit" className="text-xs font-mono">Audit & Quality</TabsTrigger>
          <TabsTrigger value="tokens" className="text-xs font-mono">Cost & Tokens</TabsTrigger>
          <TabsTrigger value="router" className="text-xs font-mono">Router</TabsTrigger>
          <TabsTrigger value="postmortem" className="text-xs font-mono">Postmortem</TabsTrigger>
        </TabsList>

        <TabsContent value="overview">
          <OverviewTab taskId={taskId} />
        </TabsContent>
        <TabsContent value="discovery">
          <DiscoveryDesignTab taskId={taskId} />
        </TabsContent>
        <TabsContent value="spec-plan">
          <SpecPlanTab taskId={taskId} />
        </TabsContent>
        <TabsContent value="execution">
          <ExecutionTab taskId={taskId} />
        </TabsContent>
        <TabsContent value="audit">
          <AuditQualityTab taskId={taskId} />
        </TabsContent>
        <TabsContent value="tokens">
          <CostTokensTab taskId={taskId} />
        </TabsContent>
        <TabsContent value="router">
          <RouterDecisionsTab taskId={taskId} />
        </TabsContent>
        <TabsContent value="postmortem">
          <PostmortemTab taskId={taskId} />
        </TabsContent>
      </Tabs>
    </div>
  );
}
