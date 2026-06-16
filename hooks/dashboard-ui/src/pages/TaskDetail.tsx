/**
 * TaskDetail.tsx — single-task forensic view.
 * Route: /repo/:slug/task/:taskId
 *
 * Styling: design-system classes only. No Tailwind.
 */

import { useState } from 'react';
import { useParams, Link } from 'react-router';
import { usePollingData, useProjectsSummary } from '../data/hooks';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface Manifest {
  task_id: string;
  title?: string;
  stage: string;
  created_at?: string;
  quality_score?: number | null;
  total_cost_usd?: number | null;
  task_type?: string;
  classification?: { task_type?: string; [k: string]: unknown };
  failure_reason?: string;
  cost_breakdown?: CostBreakdownRow[];
  [key: string]: unknown;
}

interface CostBreakdownRow {
  model?: string;
  input_tokens?: number | null;
  output_tokens?: number | null;
  estimated_usd?: number | null;
  [k: string]: unknown;
}

interface Receipt {
  filename: string;
  present?: boolean;
}

interface ReceiptsResponse {
  receipts?: Receipt[];
  expected?: string[];
}

interface AuditFinding {
  id?: string;
  severity?: string;
  title?: string;
  blocking?: boolean;
}

interface AuditReport {
  auditor_name?: string;
  findings?: AuditFinding[];
  raw?: string;
  [key: string]: unknown;
}

interface AuditSummaryResponse {
  reports?: AuditReport[];
}

interface TaskEvent {
  ts: string;
  event: string;
  detail?: string;
  to?: string;
  [key: string]: unknown;
}

interface EventsResponse {
  events?: TaskEvent[];
}

interface OptionalFile<T> {
  exists?: boolean;
  data?: T;
  raw?: string;
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------

function fmt(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
  } catch {
    return iso;
  }
}

function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleTimeString(undefined, {
      hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
    });
  } catch {
    return iso;
  }
}

function fmtCost(val: number | null | undefined, digits = 2): string {
  if (val == null || Number.isNaN(val)) return '—';
  return `$${val.toFixed(digits)}`;
}

function fmtNum(val: number | null | undefined): string {
  if (val == null || Number.isNaN(val)) return '—';
  return val.toLocaleString();
}

function shortRepoName(slug: string | undefined): string {
  if (!slug) return '—';
  const parts = slug.split('/');
  return parts[parts.length - 1] || slug;
}

// ---------------------------------------------------------------------------
// Stage badge mapping
// ---------------------------------------------------------------------------

function stageBadgeClass(stage: string): string {
  const s = stage.toUpperCase();
  if (s === 'DONE') return 'badge ok';
  if (s.includes('FAIL') || s === 'ABORTED') return 'badge err';
  if (s.startsWith('REPAIR')) return 'badge warn';
  if (s.includes('AUDIT')) return 'badge info';
  if (s === 'PLANNING' || s.startsWith('SPEC_') || s === 'PLAN_REVIEW') return 'badge active';
  if (s === 'IDLE' || s === 'FOUNDRY_INITIALIZED') return 'badge idle';
  return 'badge active';
}

// ---------------------------------------------------------------------------
// Event-type chip mapping
// ---------------------------------------------------------------------------

function eventChipClass(event: string): string {
  const e = event.toLowerCase();
  if (e.includes('denied') || e.includes('fail') || e.includes('error')) return 'event-chip denied';
  if (e.includes('repair')) return 'event-chip repair';
  if (e.includes('post') || e.includes('audit')) return 'event-chip post';
  if (e.includes('stage') || e.includes('transition')) return 'event-chip stage';
  return 'event-chip';
}

// ---------------------------------------------------------------------------
// Canonical 15-stage ordering
// ---------------------------------------------------------------------------

const STAGE_ORDER = [
  'FOUNDRY_INITIALIZED',
  'CLASSIFY_AND_SPEC',
  'SPEC_NORMALIZATION',
  'SPEC_REVIEW',
  'PLANNING',
  'PLAN_REVIEW',
  'PLAN_AUDIT',
  'TDD_REVIEW',
  'PRE_EXECUTION_SNAPSHOT',
  'EXECUTION_GRAPH_BUILD',
  'EXECUTION',
  'TEST_EXECUTION',
  'CHECKPOINT_AUDIT',
  'FINAL_AUDIT',
  'REPAIR_PLANNING',
  'REPAIR_EXECUTION',
  'DONE',
  'CALIBRATED',
  'CANCELLED',
  'FAILED',
] as const;

// ---------------------------------------------------------------------------
// Quality score color
// ---------------------------------------------------------------------------

function qualityColor(score: number | null | undefined): string {
  if (score == null) return 'var(--dim)';
  if (score > 0.8) return 'var(--lime)';
  if (score > 0.6) return 'var(--teal)';
  if (score > 0.4) return 'var(--orange)';
  return 'var(--red)';
}

// ---------------------------------------------------------------------------
// Page-level loading skeleton
// ---------------------------------------------------------------------------

function PageLoadingSkeleton() {
  return (
    <div role="status" aria-label="Loading task details">
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <span className="breadcrumb-cur" style={{ color: 'var(--dim)' }}>…</span>
      </nav>
      <div className="page-header">
        <div className="page-header-left">
          <div className="page-eyebrow">Task</div>
          <div
            className="page-title"
            style={{
              width: 320,
              height: 28,
              background: 'var(--glass-b)',
              borderRadius: 4,
            }}
            aria-hidden="true"
          />
        </div>
      </div>
      <div className="card">
        <div className="card-body">
          <div className="loading-row">Loading task…</div>
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page-level error state
// ---------------------------------------------------------------------------

interface PageErrorProps {
  message: string;
  slug?: string;
  onRetry?: () => void;
}

function PageError({ message, slug, onRetry }: PageErrorProps) {
  return (
    <div role="alert">
      <div className="alert-bar --crit">
        <span className="alert-dot" aria-hidden="true" />
        <span style={{ flex: 1 }}>{message}</span>
        {onRetry && (
          <button
            className="btn --ghost --sm"
            onClick={onRetry}
            aria-label="Retry loading task"
            style={{ marginLeft: 12, flexShrink: 0 }}
          >
            Retry
          </button>
        )}
      </div>
      <Link
        to={slug ? `/repo/${slug}` : '/'}
        className="btn --ghost --sm"
        aria-label={slug ? `Back to ${slug}` : 'Back to home'}
        style={{ marginTop: 12, display: 'inline-block' }}
      >
        ← {slug ? `Back to ${shortRepoName(slug)}` : 'Back to home'}
      </Link>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 404 not-found state
// ---------------------------------------------------------------------------

function TaskNotFound({ taskId, slug }: { taskId: string; slug: string }) {
  return (
    <div
      role="main"
      aria-label="Task not found"
      style={{
        minHeight: '60vh',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 16,
        padding: 48,
        textAlign: 'center',
      }}
    >
      <div
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 48,
          color: 'var(--dim)',
          letterSpacing: '0.05em',
        }}
      >
        404
      </div>
      <p style={{ margin: 0, fontSize: 18, fontWeight: 500 }}>Task not found</p>
      <p
        style={{
          margin: 0,
          color: 'var(--dim)',
          fontFamily: 'var(--font-mono)',
          fontSize: 13,
          wordBreak: 'break-all',
          maxWidth: 480,
        }}
      >
        {taskId || '(no task id)'}
      </p>
      <p style={{ margin: 0, color: 'var(--dim)', fontSize: 13, maxWidth: 360 }}>
        No manifest exists for this task ID in this repository.
      </p>
      <Link
        to={`/repo/${slug}`}
        className="btn --ghost --sm"
        aria-label={`Back to ${slug}`}
      >
        ← Back to {shortRepoName(slug)}
      </Link>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Alert bar — conditional based on stage
// ---------------------------------------------------------------------------

function AlertBar({ stage, failureReason }: { stage: string; failureReason?: string }) {
  const s = stage.toUpperCase();

  if (s.includes('FAIL') || s === 'ABORTED') {
    return (
      <div className="alert-bar --crit" role="alert">
        <span className="alert-dot" aria-hidden="true" />
        <span>
          Task failed
          {failureReason && (
            <>
              {' — '}
              <span style={{ fontFamily: 'var(--font-mono)', fontSize: 12 }}>{failureReason}</span>
            </>
          )}
        </span>
      </div>
    );
  }

  if (s === 'DONE') {
    return (
      <div className="alert-bar --ok" role="status">
        <span className="alert-dot" aria-hidden="true" />
        <span>Task completed successfully</span>
      </div>
    );
  }

  if (s.startsWith('REPAIR')) {
    return (
      <div className="alert-bar --warn" role="status">
        <span className="alert-dot" aria-hidden="true" />
        <span>Repair cycle in progress</span>
      </div>
    );
  }

  return null;
}

// ---------------------------------------------------------------------------
// 4-tile stats bar
// ---------------------------------------------------------------------------

function StatsBar({ manifest }: { manifest: Manifest }) {
  const quality = manifest.quality_score;
  const cost = manifest.total_cost_usd;
  const taskType =
    manifest.classification?.task_type || manifest.task_type || '—';

  return (
    <div className="stats-bar" role="region" aria-label="Task summary">
      {/* Stage */}
      <div className="stat-tile">
        <span className="stat-label">Stage</span>
        <span className="stat-value">
          <span className={stageBadgeClass(manifest.stage)}>{manifest.stage}</span>
        </span>
      </div>

      {/* Quality Score */}
      <div className="stat-tile">
        <span className="stat-label">Quality Score</span>
        <span
          className="stat-value"
          style={{ color: qualityColor(quality), fontFamily: 'var(--font-mono)' }}
          aria-label={`Quality score: ${quality != null ? quality.toFixed(3) : 'unavailable'}`}
        >
          {quality != null ? quality.toFixed(3) : '—'}
        </span>
      </div>

      {/* Est. Cost */}
      <div className="stat-tile">
        <span className="stat-label">Est. Cost</span>
        <span
          className="stat-value cost-val"
          style={{ color: 'var(--lime)', fontFamily: 'var(--font-mono)' }}
          aria-label={`Estimated cost: ${cost != null ? fmtCost(cost) : 'unavailable'}`}
        >
          {cost != null ? fmtCost(cost) : '—'}
        </span>
      </div>

      {/* Task Type */}
      <div className="stat-tile">
        <span className="stat-label">Task Type</span>
        <span
          className="stat-value"
          style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 14,
            color: 'var(--bone)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            maxWidth: '100%',
          }}
          title={taskType}
        >
          {taskType}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page tabs
// ---------------------------------------------------------------------------

type TabId = 'overview' | 'receipts' | 'audit' | 'events' | 'raw';

const TABS: { id: TabId; label: string }[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'receipts', label: 'Receipts' },
  { id: 'audit',    label: 'Audit' },
  { id: 'events',   label: 'Events' },
  { id: 'raw',      label: 'Raw' },
];

function PageTabs({ active, onChange }: { active: TabId; onChange: (t: TabId) => void }) {
  return (
    <div className="page-tabs" role="tablist" aria-label="Task sections">
      {TABS.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={active === t.id}
          aria-controls={`panel-${t.id}`}
          id={`tab-${t.id}`}
          className={'page-tab' + (active === t.id ? ' active' : '')}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Overview tab — Metadata + Stage Timeline + Cost Breakdown
// ---------------------------------------------------------------------------

function MetadataCard({ manifest }: { manifest: Manifest }) {
  const quality = manifest.quality_score;
  const cost = manifest.total_cost_usd;
  const taskType =
    manifest.classification?.task_type || manifest.task_type || '—';

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Metadata</span>
      </div>
      <div className="card-body">
        <div className="kv">
          <div className="kv-key">task_id</div>
          <div
            className="kv-val"
            style={{ fontFamily: 'var(--font-mono)', wordBreak: 'break-all' }}
          >
            {manifest.task_id}
          </div>

          <div className="kv-key">title</div>
          <div className="kv-val" style={{ wordBreak: 'break-word' }}>
            {manifest.title || '—'}
          </div>

          <div className="kv-key">stage</div>
          <div className="kv-val">
            <span className={stageBadgeClass(manifest.stage)}>{manifest.stage}</span>
          </div>

          <div className="kv-key">created</div>
          <div className="kv-val" style={{ fontFamily: 'var(--font-mono)' }}>
            {fmt(manifest.created_at)}
          </div>

          <div className="kv-key">quality</div>
          <div
            className="kv-val"
            style={{ fontFamily: 'var(--font-mono)', color: qualityColor(quality) }}
          >
            {quality != null ? quality.toFixed(3) : '—'}
          </div>

          <div className="kv-key">cost</div>
          <div
            className="kv-val cost-val"
            style={{ fontFamily: 'var(--font-mono)', color: 'var(--lime)' }}
          >
            {cost != null ? fmtCost(cost, 4) : '—'}
          </div>

          <div className="kv-key">type</div>
          <div className="kv-val" style={{ fontFamily: 'var(--font-mono)' }}>
            {taskType}
          </div>

          <div className="kv-key">task_type</div>
          <div className="kv-val" style={{ fontFamily: 'var(--font-mono)' }}>
            {manifest.task_type || '—'}
          </div>
        </div>
      </div>
    </div>
  );
}

function StageTimelineCard({
  manifest,
  events,
  eventsLoading,
}: {
  manifest: Manifest;
  events: EventsResponse | null;
  eventsLoading: boolean;
}) {
  const currentStage = manifest.stage.toUpperCase();
  const isFailed = currentStage.includes('FAIL') || currentStage === 'ABORTED';

  // Map of stage_name → ts from stage_transition events
  const timestamps: Record<string, string> = {};
  for (const ev of events?.events ?? []) {
    if (ev.event === 'stage_transition' && typeof ev.to === 'string') {
      timestamps[ev.to] = ev.ts;
    }
  }

  const currentIdx = STAGE_ORDER.indexOf(currentStage as typeof STAGE_ORDER[number]);

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Stage Timeline</span>
        {eventsLoading && !events && (
          <span
            style={{
              fontSize: 11,
              color: 'var(--dim)',
              fontFamily: 'var(--font-mono)',
            }}
          >
            loading…
          </span>
        )}
      </div>
      <div className="card-body">
        <ol
          className="stage-timeline"
          style={{ listStyle: 'none', margin: 0, padding: 0 }}
        >
          {STAGE_ORDER.map((stage, idx) => {
            const isPast = currentIdx >= 0 && idx < currentIdx;
            const isCurrent = stage === currentStage;
            const isFailedHere = isFailed && isCurrent;

            let dotClass = 'stage-dot';
            if (isFailedHere) dotClass = 'stage-dot failed';
            else if (isCurrent) dotClass = 'stage-dot current';
            else if (isPast) dotClass = 'stage-dot done';

            const ts = timestamps[stage];

            return (
              <li key={stage} className="stage-row">
                <span className={dotClass} aria-hidden="true" />
                <span
                  className="stage-name"
                  style={{
                    color: isFailedHere
                      ? 'var(--red)'
                      : isCurrent
                      ? 'var(--bone)'
                      : isPast
                      ? 'var(--bone)'
                      : 'var(--dim)',
                    fontWeight: isCurrent ? 600 : 400,
                  }}
                >
                  {stage.replace(/_/g, ' ')}
                </span>
                <span
                  className="stage-ts"
                  style={{
                    fontFamily: 'var(--font-mono)',
                    fontSize: 11,
                    color: 'var(--dim)',
                  }}
                >
                  {ts ? fmtTime(ts) : (isPast || isCurrent) ? '—' : ''}
                </span>
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}

function CostBreakdownCard({ manifest }: { manifest: Manifest }) {
  const rows = manifest.cost_breakdown ?? [];

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Cost Breakdown</span>
        {rows.length > 0 && (
          <span
            style={{
              fontSize: 12,
              fontFamily: 'var(--font-mono)',
              color: 'var(--dim)',
            }}
          >
            {rows.length} model{rows.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {rows.length === 0 ? (
        <div className="card-body">
          <div className="empty-state" role="status">
            No cost breakdown recorded for this task yet.
          </div>
        </div>
      ) : (
        <div className="card-body--flush">
          <div className="table-wrap">
            <table className="dt" aria-label="Cost breakdown by model">
              <thead>
                <tr>
                  <th scope="col">Model</th>
                  <th scope="col" style={{ textAlign: 'right' }}>Input tok</th>
                  <th scope="col" style={{ textAlign: 'right' }}>Output tok</th>
                  <th scope="col" style={{ textAlign: 'right' }}>Est. USD</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, idx) => (
                  <tr key={`${row.model ?? 'unknown'}-${idx}`}>
                    <td className="col-mono">{row.model || '—'}</td>
                    <td className="col-mono col-dim" style={{ textAlign: 'right' }}>
                      {fmtNum(row.input_tokens)}
                    </td>
                    <td className="col-mono col-dim" style={{ textAlign: 'right' }}>
                      {fmtNum(row.output_tokens)}
                    </td>
                    <td
                      className="col-mono cost-val"
                      style={{ textAlign: 'right', color: 'var(--lime)' }}
                    >
                      {row.estimated_usd != null ? fmtCost(row.estimated_usd, 4) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Receipts tab
// ---------------------------------------------------------------------------

function ReceiptsCard({
  receipts,
  loading,
  error,
  onRetry,
}: {
  receipts: ReceiptsResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const list = receipts?.receipts ?? [];
  const expected = receipts?.expected ?? [];

  const presentSet = new Set(list.map((r) => r.filename.toLowerCase()));
  const allNames = [
    ...list.map((r) => r.filename),
    ...expected.filter((e) => !presentSet.has(e.toLowerCase())),
  ];

  const presentCount = list.length;
  const expectedCount = Math.max(allNames.length, expected.length);

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Trust Receipts</span>
        {receipts != null && (
          <span
            style={{
              fontSize: 12,
              fontFamily: 'var(--font-mono)',
              color: presentCount < expectedCount ? 'var(--red)' : 'var(--lime)',
            }}
          >
            {presentCount} present / {expectedCount} expected
          </span>
        )}
      </div>

      {loading && !receipts && (
        <div className="card-body">
          <div className="loading-row">Loading receipts…</div>
        </div>
      )}

      {error && !receipts && (
        <div className="card-body" role="alert">
          <div className="alert-bar --crit" style={{ marginBottom: 0 }}>
            <span className="alert-dot" aria-hidden="true" />
            <span style={{ flex: 1 }}>Failed to load receipts: {error}</span>
            <button
              className="btn --ghost --sm"
              onClick={onRetry}
              aria-label="Retry loading receipts"
            >
              Retry
            </button>
          </div>
        </div>
      )}

      {receipts != null && allNames.length === 0 && (
        <div className="card-body">
          <div className="empty-state" role="status">
            No receipts expected for this task.
          </div>
        </div>
      )}

      {receipts != null && allNames.length > 0 && (
        <div className="card-body--flush">
          <div className="table-wrap">
            <table className="dt" aria-label="Trust receipts">
              <thead>
                <tr>
                  <th scope="col">Name</th>
                  <th scope="col" style={{ width: 80, textAlign: 'center' }}>Status</th>
                </tr>
              </thead>
              <tbody>
                {allNames.map((name) => {
                  const present = presentSet.has(name.toLowerCase());
                  return (
                    <tr key={name}>
                      <td className="col-mono">{name}</td>
                      <td style={{ textAlign: 'center' }}>
                        <span
                          aria-label={present ? 'Present' : 'Missing'}
                          style={{
                            fontFamily: 'var(--font-mono)',
                            fontWeight: 700,
                            fontSize: 14,
                            color: present ? 'var(--teal)' : 'var(--red)',
                          }}
                        >
                          {present ? '✓' : '✗'}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Audit tab
// ---------------------------------------------------------------------------

function AuditTab({
  auditSummary,
  loading,
  error,
  onRetry,
}: {
  auditSummary: AuditSummaryResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const reports = auditSummary?.reports ?? [];

  return (
    <>
      <div className="section-label">── AUDIT REPORTS</div>

      {loading && !auditSummary && (
        <div className="card">
          <div className="card-body">
            <div className="loading-row">Loading audit reports…</div>
          </div>
        </div>
      )}

      {error && !auditSummary && (
        <div role="alert">
          <div className="alert-bar --crit">
            <span className="alert-dot" aria-hidden="true" />
            <span style={{ flex: 1 }}>Failed to load audit reports: {error}</span>
            <button
              className="btn --ghost --sm"
              onClick={onRetry}
              aria-label="Retry loading audit reports"
            >
              Retry
            </button>
          </div>
        </div>
      )}

      {auditSummary != null && reports.length === 0 && (
        <div className="card">
          <div className="card-body">
            <div className="empty-state" role="status">
              No audit reports generated yet.
            </div>
          </div>
        </div>
      )}

      {auditSummary != null && reports.length > 0 && (
        <div className="card">
          <div className="card-body">
            {reports.map((report, idx) => {
              const name = report.auditor_name ?? `Report ${idx + 1}`;
              const findings = report.findings ?? [];
              const blockingCount = findings.filter((f) => f.blocking).length;
              const totalCount = findings.length;
              const color =
                blockingCount > 0
                  ? 'var(--red)'
                  : totalCount > 0
                  ? 'var(--orange)'
                  : 'var(--lime)';
              const raw = report.raw ?? JSON.stringify(report, null, 2);

              return (
                <details key={idx} className="collapse">
                  <summary>
                    <span style={{ flex: 1 }}>{name}</span>
                    <span
                      style={{
                        color,
                        fontSize: 11,
                        marginLeft: 8,
                        fontFamily: 'var(--font-mono)',
                      }}
                    >
                      {totalCount} finding{totalCount !== 1 ? 's' : ''}
                      {blockingCount > 0 && ` (${blockingCount} blocking)`}
                    </span>
                  </summary>
                  <pre>{raw}</pre>
                </details>
              );
            })}
          </div>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Events tab
// ---------------------------------------------------------------------------

function EventsTab({
  events,
  loading,
  error,
  onRetry,
}: {
  events: EventsResponse | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const list = events?.events ?? [];

  return (
    <div className="card">
      <div className="card-header">
        <span className="card-title">Events</span>
        {events != null && (
          <span
            style={{
              fontSize: 12,
              fontFamily: 'var(--font-mono)',
              color: 'var(--dim)',
            }}
          >
            {list.length} event{list.length !== 1 ? 's' : ''}
          </span>
        )}
      </div>

      {loading && !events && (
        <div className="card-body">
          <div className="loading-row">Loading events…</div>
        </div>
      )}

      {error && !events && (
        <div className="card-body" role="alert">
          <div className="alert-bar --crit" style={{ marginBottom: 0 }}>
            <span className="alert-dot" aria-hidden="true" />
            <span style={{ flex: 1 }}>Failed to load events: {error}</span>
            <button
              className="btn --ghost --sm"
              onClick={onRetry}
              aria-label="Retry loading events"
            >
              Retry
            </button>
          </div>
        </div>
      )}

      {events != null && list.length === 0 && (
        <div className="card-body">
          <div className="empty-state" role="status" aria-live="polite">
            No events recorded yet.
          </div>
        </div>
      )}

      {events != null && list.length > 0 && (
        <div className="card-body--flush">
          <div
            className="table-wrap"
            style={{ maxHeight: 540, overflowY: 'auto' }}
            role="log"
            aria-label="Task events"
            aria-live="polite"
          >
            <table className="dt" aria-label="Events table">
              <thead>
                <tr>
                  <th scope="col" style={{ width: 90 }}>Time</th>
                  <th scope="col" style={{ width: 130 }}>Type</th>
                  <th scope="col">Event</th>
                  <th scope="col">Detail</th>
                </tr>
              </thead>
              <tbody>
                {list.map((ev, idx) => {
                  const detail = ev.detail != null ? String(ev.detail) : '—';
                  return (
                    <tr key={`${ev.ts}-${idx}`}>
                      <td
                        className="col-mono col-dim"
                        style={{ whiteSpace: 'nowrap', fontSize: 11 }}
                      >
                        {fmtTime(ev.ts)}
                      </td>
                      <td>
                        <span className={eventChipClass(ev.event)}>
                          {ev.event.replace(/_/g, ' ')}
                        </span>
                      </td>
                      <td
                        className="col-mono"
                        style={{
                          maxWidth: 260,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                        }}
                        title={ev.event}
                      >
                        {ev.event}
                      </td>
                      <td
                        className="col-mono col-dim"
                        style={{
                          maxWidth: 360,
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap',
                          fontSize: 11,
                        }}
                        title={detail !== '—' ? detail : undefined}
                      >
                        {detail}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Raw tab
// ---------------------------------------------------------------------------

interface RawTabProps {
  manifest: Manifest;
  repairLog: OptionalFile<unknown> | null;
  executionGraph: OptionalFile<unknown> | null;
  repairLoading: boolean;
  executionGraphLoading: boolean;
}

function rawString(file: OptionalFile<unknown> | null): string {
  if (!file) return '';
  if (typeof file.raw === 'string' && file.raw.length > 0) return file.raw;
  if (file.data != null) return JSON.stringify(file.data, null, 2);
  return '';
}

function fileExists(file: OptionalFile<unknown> | null): boolean {
  if (!file) return false;
  if (file.exists === false) return false;
  return file.data != null || (typeof file.raw === 'string' && file.raw.length > 0);
}

function RawTab({
  manifest,
  repairLog,
  executionGraph,
  repairLoading,
  executionGraphLoading,
}: RawTabProps) {
  const manifestText = JSON.stringify(manifest, null, 2);
  const repairExists = fileExists(repairLog);
  const graphExists = fileExists(executionGraph);

  return (
    <div className="card">
      <div className="card-body">
        {/* manifest.json */}
        <div className="section-label">── manifest.json</div>
        <details className="collapse">
          <summary>manifest.json ({manifestText.length.toLocaleString()} bytes)</summary>
          <pre>{manifestText}</pre>
        </details>

        <div className="divider" />

        {/* repair-log.json */}
        <div className="section-label">── repair-log.json</div>
        {repairLoading && !repairLog ? (
          <div className="loading-row">Loading repair log…</div>
        ) : repairExists ? (
          <details className="collapse">
            <summary>repair-log.json</summary>
            <pre>{rawString(repairLog)}</pre>
          </details>
        ) : (
          <div className="empty-state" role="status">
            No repair log for this task.
          </div>
        )}

        <div className="divider" />

        {/* execution-graph.json */}
        <div className="section-label">── execution-graph.json</div>
        {executionGraphLoading && !executionGraph ? (
          <div className="loading-row">Loading execution graph…</div>
        ) : graphExists ? (
          <details className="collapse">
            <summary>execution-graph.json</summary>
            <pre>{rawString(executionGraph)}</pre>
          </details>
        ) : (
          <div className="empty-state" role="status">
            No execution graph for this task.
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function TaskDetail() {
  const { slug, taskId } = useParams<{ slug: string; taskId: string }>();
  const [activeTab, setActiveTab] = useState<TabId>('overview');

  // Step 1 — resolve slug to project path
  const {
    data: projects,
    loading: projLoading,
    error: projError,
    refetch: projRefetch,
  } = useProjectsSummary(30000);

  const projectPath =
    projects && slug ? projects.find((p) => p.slug === slug)?.path ?? null : null;

  function taskUrl(endpoint: string, extra = ''): string {
    if (!projectPath || !taskId) return '';
    return `/api/${endpoint}?project=${encodeURIComponent(
      projectPath,
    )}&task=${encodeURIComponent(taskId)}${extra}`;
  }

  // Manifest — 5s polling
  const {
    data: manifest,
    loading: manifestLoading,
    error: manifestError,
    refetch: manifestRefetch,
  } = usePollingData<Manifest>(taskUrl('task-manifest'), 5000, { globalScope: true });

  // Audit summary — 10s
  const {
    data: auditSummary,
    loading: auditLoading,
    error: auditError,
    refetch: auditRefetch,
  } = usePollingData<AuditSummaryResponse>(taskUrl('audit-summary'), 10000, {
    globalScope: true,
  });

  // Receipts — 10s
  const {
    data: receipts,
    loading: receiptsLoading,
    error: receiptsError,
    refetch: receiptsRefetch,
  } = usePollingData<ReceiptsResponse>(taskUrl('receipts'), 10000, {
    globalScope: true,
  });

  // Events — 5s, limit 100
  const {
    data: events,
    loading: eventsLoading,
    error: eventsError,
    refetch: eventsRefetch,
  } = usePollingData<EventsResponse>(taskUrl('events', '&limit=100'), 5000, {
    globalScope: true,
  });

  // Repair log — 30s (only used in Raw tab)
  const {
    data: repairLog,
    loading: repairLogLoading,
  } = usePollingData<OptionalFile<unknown>>(taskUrl('repair-log'), 30000, {
    globalScope: true,
  });

  // Execution graph — 30s (only used in Raw tab)
  const {
    data: executionGraph,
    loading: executionGraphLoading,
  } = usePollingData<OptionalFile<unknown>>(taskUrl('execution-graph'), 30000, {
    globalScope: true,
  });

  // -------------------------------------------------------------------------
  // Guards
  // -------------------------------------------------------------------------
  if (projLoading && !projects) {
    return <PageLoadingSkeleton />;
  }

  if (projError && !projects) {
    return (
      <PageError
        message={`Unable to load project registry: ${projError}`}
        slug={slug}
        onRetry={projRefetch}
      />
    );
  }

  if (!projLoading && projects && !projectPath) {
    return (
      <PageError
        message={`No project found for slug: ${slug ?? '(none)'}`}
        slug={undefined}
      />
    );
  }

  if (manifestError && !manifest) {
    return (
      <PageError
        message={`Failed to load task manifest: ${manifestError}`}
        slug={slug}
        onRetry={manifestRefetch}
      />
    );
  }

  if (manifestLoading && !manifest) {
    return <PageLoadingSkeleton />;
  }

  if (!manifest) {
    return <TaskNotFound taskId={taskId ?? ''} slug={slug ?? ''} />;
  }

  // -------------------------------------------------------------------------
  // Happy path
  // -------------------------------------------------------------------------

  function refreshAll() {
    manifestRefetch();
    eventsRefetch();
    auditRefetch();
    receiptsRefetch();
  }

  return (
    <div role="main" aria-label={`Task ${manifest.task_id}`}>
      {/* Breadcrumb: home / {repoShortName} / {taskId} */}
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <Link to="/" aria-label="Home">home</Link>
        <span className="breadcrumb-sep" aria-hidden="true">/</span>
        <Link to={`/repo/${slug}`} aria-label={`Repository ${slug}`}>
          {shortRepoName(slug)}
        </Link>
        <span className="breadcrumb-sep" aria-hidden="true">/</span>
        <span
          className="breadcrumb-cur"
          aria-current="page"
          title={manifest.task_id}
          style={{
            fontFamily: 'var(--font-mono)',
            maxWidth: 'min(50vw, 320px)',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            display: 'inline-block',
            verticalAlign: 'bottom',
          }}
        >
          {manifest.task_id}
        </span>
      </nav>

      {/* Page header */}
      <div className="page-header">
        <div className="page-header-left">
          <span className="page-eyebrow">Task</span>
          <h1
            className="page-title"
            title={manifest.task_id}
            style={{
              fontFamily: 'var(--font-mono)',
              letterSpacing: '-0.01em',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
              maxWidth: 'min(70vw, 720px)',
            }}
          >
            {manifest.task_id}
          </h1>
          {manifest.title && (
            <div
              style={{
                marginTop: 4,
                fontSize: 13,
                color: 'var(--gray)',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
                maxWidth: 'min(80vw, 720px)',
              }}
              title={manifest.title}
            >
              {manifest.title}
            </div>
          )}
        </div>
        <div className="page-header-actions">
          <span className={stageBadgeClass(manifest.stage)}>{manifest.stage}</span>
          <button
            className="btn --ghost --sm"
            onClick={refreshAll}
            disabled={manifestLoading}
            aria-label="Refresh task data"
          >
            {manifestLoading ? 'Refreshing…' : '↺ Refresh'}
          </button>
        </div>
      </div>

      {/* Alert bar */}
      <AlertBar stage={manifest.stage} failureReason={manifest.failure_reason} />

      {/* 4-tile stats bar */}
      <StatsBar manifest={manifest} />

      {/* Tabs */}
      <PageTabs active={activeTab} onChange={setActiveTab} />

      {/* Tab panels */}
      {activeTab === 'overview' && (
        <div
          role="tabpanel"
          id="panel-overview"
          aria-labelledby="tab-overview"
        >
          <MetadataCard manifest={manifest} />
          <StageTimelineCard
            manifest={manifest}
            events={events}
            eventsLoading={eventsLoading}
          />
          <CostBreakdownCard manifest={manifest} />
        </div>
      )}

      {activeTab === 'receipts' && (
        <div
          role="tabpanel"
          id="panel-receipts"
          aria-labelledby="tab-receipts"
        >
          <ReceiptsCard
            receipts={receipts}
            loading={receiptsLoading}
            error={receiptsError}
            onRetry={receiptsRefetch}
          />
        </div>
      )}

      {activeTab === 'audit' && (
        <div
          role="tabpanel"
          id="panel-audit"
          aria-labelledby="tab-audit"
        >
          <AuditTab
            auditSummary={auditSummary}
            loading={auditLoading}
            error={auditError}
            onRetry={auditRefetch}
          />
        </div>
      )}

      {activeTab === 'events' && (
        <div
          role="tabpanel"
          id="panel-events"
          aria-labelledby="tab-events"
        >
          <EventsTab
            events={events}
            loading={eventsLoading}
            error={eventsError}
            onRetry={eventsRefetch}
          />
        </div>
      )}

      {activeTab === 'raw' && (
        <div
          role="tabpanel"
          id="panel-raw"
          aria-labelledby="tab-raw"
        >
          <RawTab
            manifest={manifest}
            repairLog={repairLog}
            executionGraph={executionGraph}
            repairLoading={repairLogLoading}
            executionGraphLoading={executionGraphLoading}
          />
        </div>
      )}
    </div>
  );
}
