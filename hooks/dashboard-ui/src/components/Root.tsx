import { useState, useEffect } from "react";
import { Outlet, NavLink, useLocation } from "react-router";
import {
  LayoutDashboard,
  ListChecks,
  Bot,
  Cpu,
  BarChart3,
  Settings,
  Activity,
  Terminal,
  GitBranch,
} from "lucide-react";
import { motion, AnimatePresence } from "motion/react";
import { useProject } from "../data/ProjectContext";

const NAV_ITEMS = [
  { path: "/", icon: LayoutDashboard, label: "DASHBOARD" },
  { path: "/tasks", icon: ListChecks, label: "TASK PIPELINE" },
  { path: "/agents", icon: Bot, label: "AGENTS" },
  { path: "/learning-ops", icon: Cpu, label: "LEARNING OPS" },
  { path: "/analytics", icon: BarChart3, label: "ANALYTICS" },
  { path: "/settings", icon: Settings, label: "SETTINGS" },
  { path: "/terminal", icon: Terminal, label: "TERMINAL" },
  { path: "/graph", icon: GitBranch, label: "GRAPH" },
] as const;

const GLOBAL_VALUE = "__global__";

function formatElapsed(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600);
  const m = Math.floor((totalSeconds % 3600) / 60);
  const s = totalSeconds % 60;
  return [h, m, s].map((v) => String(v).padStart(2, "0")).join(":");
}

function UptimeCounter() {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const id = setInterval(() => {
      setElapsed((prev) => prev + 1);
    }, 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <span
      className="font-mono text-[10px] text-[#7A776E] tabular-nums whitespace-nowrap"
      role="timer"
      aria-label={`Uptime ${formatElapsed(elapsed)}`}
    >
      UPTIME {formatElapsed(elapsed)}
    </span>
  );
}

function ProjectSwitcher() {
  const { selectedProject, setSelectedProject, projects } = useProject();

  return (
    <select
      value={selectedProject}
      onChange={(e) => setSelectedProject(e.target.value)}
      aria-label="Select project"
      className="bg-[#0F1114] border border-[#BDF000]/20 text-[#BDF000] font-mono text-xs rounded px-2 py-1 outline-none focus:border-[#BDF000]/50 transition-colors max-w-[200px] truncate"
    >
      <option value={GLOBAL_VALUE}>{"\u25C8 GLOBAL OVERVIEW"}</option>
      {projects.map((p) => {
        const basename = p.path.split("/").filter(Boolean).pop() ?? p.path;
        return (
          <option key={p.path} value={p.path}>
            {basename}
          </option>
        );
      })}
    </select>
  );
}

function DaemonStatus({ active = true }: { active?: boolean }) {
  return (
    <div className="flex items-center gap-2" role="status" aria-label={active ? "Daemon active" : "Daemon idle"}>
      {active ? (
        <span className="relative flex h-3 w-3">
          <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#2DD4A8] opacity-75" />
          <span className="relative inline-flex rounded-full h-3 w-3 bg-[#2DD4A8]" />
        </span>
      ) : (
        <span className="inline-flex rounded-full h-3 w-3 bg-slate-500" />
      )}
      <span className={`text-xs font-mono ${active ? "text-[#2DD4A8]" : "text-slate-500"}`}>
        {active ? "DAEMON ACTIVE" : "DAEMON IDLE"}
      </span>
    </div>
  );
}

function isNavActive(itemPath: string, currentPath: string): boolean {
  if (itemPath === "/") return currentPath === "/";
  return currentPath.startsWith(itemPath);
}

export default function Root() {
  const location = useLocation();

  return (
    <div className="min-h-screen bg-[#0F1114] text-[#F0F0E8] font-sans overflow-hidden flex flex-col relative selection:bg-[#BDF000]/30">
      {/* Cosmic Background */}
      <div className="cosmic-bg" aria-hidden="true" />

      {/* Header */}
      <header className="relative z-20 flex items-center justify-between px-4 sm:px-6 py-4 border-b border-white/6 bg-[#0F1114]/80 backdrop-blur-md">
        <div className="flex items-center gap-3 min-w-0">
          <Activity className="w-5 h-5 text-[#BDF000] shrink-0" aria-hidden="true" />
          <span className="font-mono text-xs font-semibold text-[#BDF000] tracking-widest whitespace-nowrap hidden sm:inline">
            DYNOS-WORK // DASHBOARD
          </span>
          <span className="font-mono text-xs font-semibold text-[#BDF000] tracking-widest sm:hidden">
            DYNOS
          </span>
        </div>

        <div className="flex items-center gap-4 sm:gap-6">
          <ProjectSwitcher />
          <UptimeCounter />
          <DaemonStatus />
        </div>
      </header>

      {/* Layout Shell */}
      <div className="flex-1 flex relative z-10 overflow-hidden">
        {/* Sidebar */}
        <nav
          className="hidden md:flex flex-col items-center w-16 lg:w-20 border-r border-white/6 bg-[#0F1114]/60 backdrop-blur-sm py-6 gap-8 z-20 shrink-0"
          aria-label="Main navigation"
        >
          {NAV_ITEMS.map((item) => {
            const active = isNavActive(item.path, location.pathname);
            return (
              <NavLink
                key={item.path}
                to={item.path}
                className="relative group flex items-center justify-center w-full"
                aria-label={item.label}
                end={item.path === "/"}
              >
                <div
                  className={`p-3 rounded-xl transition-all duration-300 ${
                    active
                      ? "bg-[#BDF000]/10 shadow-[0_0_15px_rgba(189,240,0,0.1)]"
                      : "hover:bg-white/5"
                  }`}
                >
                  <item.icon
                    className={`w-5 h-5 transition-colors ${
                      active
                        ? "text-[#BDF000]"
                        : "text-slate-500 group-hover:text-[#2DD4A8]"
                    }`}
                    aria-hidden="true"
                  />
                </div>
                {active && (
                  <motion.div
                    layoutId="activeNav"
                    className="absolute right-0 top-1/2 -translate-y-1/2 w-1 h-8 bg-[#BDF000] rounded-l shadow-[0_0_8px_rgba(189,240,0,0.8)]"
                  />
                )}
              </NavLink>
            );
          })}
        </nav>

        {/* Mobile Bottom Bar */}
        <nav
          className="md:hidden absolute bottom-0 left-0 right-0 h-14 border-t border-white/6 bg-[#0F1114]/90 backdrop-blur-md z-30 flex items-center overflow-x-auto flex-nowrap px-2 gap-1 scrollbar-none"
          aria-label="Mobile navigation"
        >
          {NAV_ITEMS.map((item) => {
            const active = isNavActive(item.path, location.pathname);
            return (
              <NavLink
                key={item.path}
                to={item.path}
                className="relative p-2 shrink-0 flex flex-col items-center"
                aria-label={item.label}
                end={item.path === "/"}
              >
                <item.icon
                  className={`w-5 h-5 transition-colors ${
                    active ? "text-[#BDF000]" : "text-slate-500"
                  }`}
                  aria-hidden="true"
                />
                {active && (
                  <motion.div
                    layoutId="activeNavMobile"
                    className="absolute -bottom-0.5 left-1/2 -translate-x-1/2 w-6 h-0.5 bg-[#BDF000] rounded-t shadow-[0_0_8px_rgba(189,240,0,0.8)]"
                  />
                )}
              </NavLink>
            );
          })}
        </nav>

        {/* Page Content */}
        <main className="flex-1 overflow-x-hidden overflow-y-auto pb-14 md:pb-0 relative">
          <AnimatePresence mode="wait">
            <motion.div
              key={location.pathname}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -12 }}
              transition={{ duration: 0.28, ease: "easeOut" }}
            >
              <Outlet />
            </motion.div>
          </AnimatePresence>
        </main>
      </div>
    </div>
  );
}
