import { useNavigate } from "react-router";
import { useProjectsSummary } from "@/data/hooks";
import type { ProjectSummary } from "@/data/types";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function formatRelativeTime(isoString: string | null): string {
  if (!isoString) return "—";
  try {
    const diff = Date.now() - new Date(isoString).getTime();
    const seconds = Math.floor(diff / 1000);
    if (seconds < 60) return `${seconds}s ago`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  } catch {
    return "—";
  }
}

function formatQuality(score: number | null): string {
  if (score === null) return "—";
  return score.toFixed(1);
}

// ---------------------------------------------------------------------------
// RepoCard
// ---------------------------------------------------------------------------

interface RepoCardProps {
  project: ProjectSummary;
  onClick: () => void;
}

function RepoCard({ project, onClick }: RepoCardProps) {
  const stage = project.active_task_stage ?? "idle";
  const daemonRunning = project.daemon_running;

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Open repo ${project.name}`}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
      className="cursor-pointer rounded-lg border p-4 transition-colors focus:outline-none focus:ring-2 focus:ring-[#6ee7b7]/50"
      style={{ backgroundColor: "#111", borderColor: "#222" }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLDivElement).style.borderColor = "rgba(110,231,183,0.5)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLDivElement).style.borderColor = "#222";
      }}
    >
      {/* Name row + daemon indicator */}
      <div className="flex items-center justify-between gap-2 mb-1">
        <div
          className="font-semibold truncate"
          style={{ color: "#e5e5e5", fontSize: "0.9375rem" }}
          title={project.name}
        >
          {project.name}
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <span
            className="w-2 h-2 rounded-full"
            style={{ backgroundColor: daemonRunning ? "#6ee7b7" : "#555" }}
            aria-hidden="true"
          />
          <span
            className="text-[10px] uppercase tracking-wider"
            style={{
              color: daemonRunning ? "#6ee7b7" : "#888",
              fontFamily: "JetBrains Mono, monospace",
            }}
          >
            {daemonRunning ? "running" : "stopped"}
          </span>
        </div>
      </div>

      {/* Full path */}
      <div
        className="truncate mb-3"
        style={{
          color: "#888",
          fontSize: "0.75rem",
          fontFamily: "JetBrains Mono, monospace",
        }}
        title={project.path}
      >
        {project.path}
      </div>

      {/* Metrics row */}
      <div className="grid grid-cols-2 gap-x-4 gap-y-2 sm:grid-cols-4">
        <Metric label="Last active" value={formatRelativeTime(project.last_active_at)} />
        <Metric label="Tasks" value={String(project.task_count)} />
        <Metric label="Avg quality" value={formatQuality(project.avg_quality_score)} />
        <Metric label="Stage" value={stage} accent={stage !== "idle"} />
      </div>
    </div>
  );
}

function Metric({
  label,
  value,
  accent = false,
}: {
  label: string;
  value: string;
  accent?: boolean;
}) {
  return (
    <div>
      <div
        className="uppercase tracking-wider"
        style={{
          color: "#555",
          fontSize: "0.625rem",
          fontFamily: "JetBrains Mono, monospace",
        }}
      >
        {label}
      </div>
      <div
        style={{
          color: accent ? "#6ee7b7" : "#e5e5e5",
          fontSize: "0.8125rem",
          fontFamily: "JetBrains Mono, monospace",
        }}
      >
        {value}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Spinner
// ---------------------------------------------------------------------------

function Spinner() {
  return (
    <div
      className="flex items-center justify-center"
      style={{ minHeight: "60vh" }}
      role="status"
      aria-label="Loading repos"
    >
      <div
        className="w-8 h-8 rounded-full border-2 animate-spin"
        style={{
          borderColor: "rgba(110,231,183,0.3)",
          borderTopColor: "#6ee7b7",
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Home page
// ---------------------------------------------------------------------------

export default function Home() {
  const navigate = useNavigate();
  const { data, loading, error, refetch } = useProjectsSummary();

  // Loading: data not yet arrived
  if (loading && data === null) {
    return (
      <div style={{ backgroundColor: "#0a0a0a", minHeight: "100%", padding: "1.5rem" }}>
        <h1
          className="font-semibold mb-6"
          style={{ color: "#e5e5e5", fontSize: "1.5rem", fontFamily: "Inter, sans-serif" }}
        >
          Repos
        </h1>
        <Spinner />
      </div>
    );
  }

  // Error: fetch failed before any data arrived
  if (error && data === null) {
    return (
      <div style={{ backgroundColor: "#0a0a0a", minHeight: "100%", padding: "1.5rem" }}>
        <h1
          className="font-semibold mb-6"
          style={{ color: "#e5e5e5", fontSize: "1.5rem", fontFamily: "Inter, sans-serif" }}
        >
          Repos
        </h1>
        <div
          className="flex flex-col items-center justify-center gap-4"
          style={{ minHeight: "40vh" }}
          role="alert"
        >
          <p style={{ color: "#e5e5e5", fontFamily: "Inter, sans-serif" }}>
            Failed to load data
          </p>
          <button
            onClick={refetch}
            className="px-4 py-2 rounded-lg border transition-colors focus:outline-none focus:ring-2 focus:ring-[#6ee7b7]/50"
            style={{
              backgroundColor: "rgba(110,231,183,0.1)",
              borderColor: "rgba(110,231,183,0.3)",
              color: "#6ee7b7",
              fontFamily: "Inter, sans-serif",
              fontSize: "0.875rem",
            }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLButtonElement).style.backgroundColor =
                "rgba(110,231,183,0.2)";
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLButtonElement).style.backgroundColor =
                "rgba(110,231,183,0.1)";
            }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  const projects: ProjectSummary[] = data ?? [];

  return (
    <div style={{ backgroundColor: "#0a0a0a", minHeight: "100%", padding: "1.5rem" }}>
      <h1
        className="font-semibold mb-6"
        style={{ color: "#e5e5e5", fontSize: "1.5rem", fontFamily: "Inter, sans-serif" }}
      >
        Repos
      </h1>

      {projects.length === 0 ? (
        /* Empty state */
        <div
          className="flex flex-col items-center justify-center gap-2"
          style={{ minHeight: "40vh" }}
        >
          <p
            style={{
              color: "#888",
              fontFamily: "Inter, sans-serif",
              fontSize: "0.9375rem",
            }}
          >
            No repos registered
          </p>
          <p
            style={{
              color: "#555",
              fontFamily: "Inter, sans-serif",
              fontSize: "0.8125rem",
            }}
          >
            Add a repo to ~/.dynos/registry.json to get started.
          </p>
        </div>
      ) : (
        /* Success state: grid of cards */
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {projects.map((project) => (
            <RepoCard
              key={project.slug}
              project={project}
              onClick={() => navigate(`/repo/${project.slug}`)}
            />
          ))}
        </div>
      )}
    </div>
  );
}
