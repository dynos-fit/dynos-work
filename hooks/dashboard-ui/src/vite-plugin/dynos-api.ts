import type { Plugin } from "vite";
import * as fs from "node:fs";
import * as path from "node:path";
import { exec } from "node:child_process";
import { homedir } from "node:os";
import { URL } from "node:url";

const TASK_ID_PATTERN = /^task-\d{8}-\d{3}$/;

function computeSlug(projectPath: string): string {
  return projectPath.replace(/^\//, "").replace(/\//g, "-");
}

function jsonResponse(
  res: { statusCode: number; setHeader: (k: string, v: string) => void; end: (body: string) => void },
  statusCode: number,
  data: unknown,
): void {
  res.statusCode = statusCode;
  res.setHeader("Content-Type", "application/json");
  res.end(JSON.stringify(data));
}

function readJsonFile(filePath: string): unknown {
  const raw = fs.readFileSync(filePath, "utf-8");
  return JSON.parse(raw);
}

function readJsonFileOrDefault<T>(filePath: string, fallback: T): T {
  try {
    return readJsonFile(filePath) as T;
  } catch {
    return fallback;
  }
}

function readTextFile(filePath: string): string {
  return fs.readFileSync(filePath, "utf-8");
}

function getRegistry(): { version: number; projects: Array<{ path: string; [k: string]: unknown }>; [k: string]: unknown } {
  const registryPath = path.join(homedir(), ".dynos", "registry.json");
  return readJsonFile(registryPath) as { version: number; projects: Array<{ path: string; [k: string]: unknown }> };
}

function persistentDir(slug: string): string {
  return path.join(homedir(), ".dynos", "projects", slug);
}

function localDynosDir(projectPath: string): string {
  return path.join(projectPath, ".dynos");
}

function persistentProjectDir(projectPath: string): string {
  return persistentDir(computeSlug(projectPath));
}

function listTaskDirs(projectPath: string): string[] {
  try {
    const entries = fs.readdirSync(localDynosDir(projectPath));
    return (entries as string[]).filter((e) => TASK_ID_PATTERN.test(e));
  } catch {
    return [];
  }
}

function collectFromAllProjects<T>(
  collector: (projectPath: string, slug: string) => T[],
): T[] {
  try {
    const registry = getRegistry();
    const results: T[] = [];
    for (const proj of registry.projects) {
      try {
        const slug = computeSlug(proj.path);
        results.push(...collector(proj.path, slug));
      } catch {
        // skip projects that fail
      }
    }
    return results;
  } catch {
    return [];
  }
}

const MAX_BODY_SIZE = 1024 * 1024; // 1MB

function parseBody(req: { on: (event: string, cb: (data?: Buffer) => void) => unknown }): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let totalSize = 0;
    req.on("data", (chunk?: Buffer) => {
      if (chunk) {
        totalSize += chunk.length;
        if (totalSize > MAX_BODY_SIZE) {
          reject(new Error("Request body too large"));
          return;
        }
        chunks.push(chunk);
      }
    });
    req.on("end", () => {
      const raw = Buffer.concat(chunks).toString("utf-8");
      try {
        resolve(JSON.parse(raw));
      } catch {
        reject(new Error("Invalid JSON body"));
      }
    });
    req.on("error", (err?: Buffer) => {
      reject(err ?? new Error("Request read error"));
    });
  });
}

function atomicWriteJson(filePath: string, data: unknown): void {
  const dir = path.dirname(filePath);
  try {
    fs.mkdirSync(dir, { recursive: true });
  } catch {
    // directory may already exist
  }
  const tmpPath = filePath + ".tmp";
  fs.writeFileSync(tmpPath, JSON.stringify(data, null, 2));
  fs.renameSync(tmpPath, filePath);
}

function notNull<T>(val: T | null): val is T {
  return val !== null;
}

interface SimpleReq {
  url?: string;
  method?: string;
  on: (event: string, cb: (data?: Buffer) => void) => unknown;
}

interface SimpleRes {
  statusCode: number;
  setHeader: (k: string, v: string) => void;
  end: (body: string) => void;
}

function isRegisteredProject(projectPath: string): boolean {
  try {
    const registry = getRegistry();
    const normalized = path.resolve(projectPath);
    return registry.projects.some((p) => path.resolve(p.path) === normalized);
  } catch {
    return false;
  }
}

function listCodeFiles(target: string): string[] {
  const TEXT_EXTENSIONS = new Set([
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".md",
    ".json",
    ".yml",
    ".yaml",
    ".toml",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".rb",
    ".php",
    ".sh",
    ".css",
    ".scss",
    ".html",
  ]);

  const results: string[] = [];
  const root = path.resolve(target);
  const stack = [root];
  while (stack.length > 0) {
    const current = stack.pop();
    if (!current) continue;
    let stat: fs.Stats;
    try {
      stat = fs.statSync(current);
    } catch {
      continue;
    }
    if (stat.isSymbolicLink()) continue;
    if (stat.isFile()) {
      if (TEXT_EXTENSIONS.has(path.extname(current).toLowerCase())) {
        results.push(current);
      }
      continue;
    }
    if (!stat.isDirectory()) continue;
    const base = path.basename(current);
    if (base === ".git" || base === ".dynos" || base === "node_modules") continue;
    let entries: string[];
    try {
      entries = fs.readdirSync(current);
    } catch {
      continue;
    }
    for (const entry of entries) {
      stack.push(path.join(current, entry));
    }
  }
  return results;
}

function readTextSafe(filePath: string): string {
  return fs.readFileSync(filePath, "utf-8");
}

function collectRetrospectivesForProject(projectPath: string): Array<Record<string, unknown>> {
  return listTaskDirs(projectPath).map((taskDir) => {
    try {
      const data = readJsonFile(path.join(localDynosDir(projectPath), taskDir, "task-retrospective.json")) as Record<string, unknown>;
      return { ...data, task_id: taskDir };
    } catch {
      return null;
    }
  }).filter(notNull);
}

function buildRepoReport(projectPath: string): Record<string, unknown> {
  const registryPath = path.join(persistentProjectDir(projectPath), "learned-agents", "registry.json");
  const queuePath = path.join(persistentProjectDir(projectPath), "automation-queue.json");
  const historyPath = path.join(persistentProjectDir(projectPath), "benchmark-history.json");
  const indexPath = path.join(persistentProjectDir(projectPath), "benchmark-index.json");

  const registry = readJsonFileOrDefault<Record<string, unknown>>(registryPath, {});
  const queue = readJsonFileOrDefault<Record<string, unknown>>(queuePath, {});
  const history = readJsonFileOrDefault<Record<string, unknown>>(historyPath, {});
  const index = readJsonFileOrDefault<Record<string, unknown>>(indexPath, {});

  const agents = Array.isArray(registry.agents) ? registry.agents as Record<string, unknown>[] : [];
  const active = agents.filter((item) => Boolean(item.route_allowed));
  const shadow = agents.filter((item) => item.mode === "shadow");
  const demoted = agents.filter((item) => item.status === "demoted_on_regression");
  const queueItems = Array.isArray(queue.items) ? queue.items as Record<string, unknown>[] : [];
  const runs = Array.isArray(history.runs) ? history.runs as Record<string, unknown>[] : [];
  const fixtures = Array.isArray(index.fixtures) ? index.fixtures as Record<string, unknown>[] : [];
  const fixtureIds = new Set(
    fixtures
      .map((item) => typeof item.fixture_id === "string" ? item.fixture_id : null)
      .filter(notNull),
  );

  const uncovered = shadow
    .filter((item) => {
      const fixtureId = `${item.item_kind ?? "agent"}-${item.agent_name ?? "unknown"}-${item.task_type ?? "unknown"}`;
      return !fixtureIds.has(fixtureId);
    })
    .map((item) => ({
      target_name: String(item.agent_name ?? "unknown"),
      role: String(item.role ?? "unknown"),
      task_type: String(item.task_type ?? "unknown"),
      item_kind: String(item.item_kind ?? "agent"),
    }));

  return {
    registry_updated_at: registry.updated_at ?? null,
    summary: {
      learned_components: agents.length,
      active_routes: active.length,
      shadow_components: shadow.length,
      demoted_components: demoted.length,
      queued_automation_jobs: queueItems.length,
      benchmark_runs: runs.length,
      tracked_fixtures: fixtures.length,
      coverage_gaps: uncovered.length,
    },
    active_routes: active.map((item) => ({
      agent_name: String(item.agent_name ?? "unknown"),
      role: String(item.role ?? "unknown"),
      task_type: String(item.task_type ?? "unknown"),
      item_kind: String(item.item_kind ?? "agent"),
      mode: String(item.mode ?? "unknown"),
      composite: typeof (item.benchmark_summary as Record<string, unknown> | undefined)?.mean_composite === "number"
        ? ((item.benchmark_summary as Record<string, unknown>).mean_composite as number)
        : 0,
    })),
    demotions: demoted.map((item) => ({
      agent_name: String(item.agent_name ?? "unknown"),
      role: String(item.role ?? "unknown"),
      task_type: String(item.task_type ?? "unknown"),
      last_evaluation: (item.last_evaluation as Record<string, unknown>) ?? {},
    })),
    automation_queue: queueItems,
    coverage_gaps: uncovered,
    recent_runs: runs.slice(-5),
  };
}

function buildProjectStats(projectPath: string): Record<string, unknown> {
  const retrospectives = collectRetrospectivesForProject(projectPath);
  const taskCountsByType: Record<string, number> = {};
  const qualityScores: number[] = [];
  const executorRepairTotals: Record<string, number[]> = {};
  const preventionRules: Record<string, number> = {};
  const preventionRuleExecutors: Record<string, string> = {};

  for (const retro of retrospectives) {
    const taskType = typeof retro.task_type === "string" ? retro.task_type : "";
    if (taskType) {
      taskCountsByType[taskType] = (taskCountsByType[taskType] ?? 0) + 1;
    }

    if (typeof retro.quality_score === "number") {
      qualityScores.push(retro.quality_score);
    }

    const repairFrequency = retro.executor_repair_frequency;
    if (repairFrequency && typeof repairFrequency === "object") {
      for (const [role, count] of Object.entries(repairFrequency as Record<string, unknown>)) {
        if (typeof count === "number") {
          executorRepairTotals[role] ??= [];
          executorRepairTotals[role].push(count);
        }
      }
    }

    const rules = Array.isArray(retro.prevention_rules) ? retro.prevention_rules : [];
    for (const rule of rules) {
      if (typeof rule === "string" && rule) {
        preventionRules[rule] = (preventionRules[rule] ?? 0) + 1;
        preventionRuleExecutors[rule] ??= "unknown";
      } else if (rule && typeof rule === "object") {
        const candidate = typeof (rule as Record<string, unknown>).rule === "string"
          ? (rule as Record<string, unknown>).rule as string
          : typeof (rule as Record<string, unknown>).text === "string"
            ? (rule as Record<string, unknown>).text as string
            : "";
        if (candidate) {
          preventionRules[candidate] = (preventionRules[candidate] ?? 0) + 1;
          preventionRuleExecutors[candidate] = typeof (rule as Record<string, unknown>).executor === "string"
            ? (rule as Record<string, unknown>).executor as string
            : "unknown";
        }
      }
    }
  }

  const executorReliability = Object.fromEntries(
    Object.entries(executorRepairTotals).map(([role, counts]) => {
      const averageRepairs = counts.length > 0 ? counts.reduce((sum, count) => sum + count, 0) / counts.length : 0;
      return [role, Number(Math.max(0, 1 - averageRepairs * 0.1).toFixed(3))];
    }),
  );

  return {
    total_tasks: Object.values(taskCountsByType).reduce((sum, value) => sum + value, 0),
    task_counts_by_type: taskCountsByType,
    average_quality_score: Number((qualityScores.length > 0 ? qualityScores.reduce((sum, score) => sum + score, 0) / qualityScores.length : 0).toFixed(3)),
    executor_reliability: executorReliability,
    prevention_rule_frequencies: preventionRules,
    prevention_rule_executors: preventionRuleExecutors,
  };
}

function buildRepoState(projectPath: string): Record<string, unknown> {
  const files = listCodeFiles(projectPath);
  let totalLines = 0;
  let importCount = 0;
  let controlFlowCount = 0;
  let symbols = 0;
  const languages: Record<string, number> = {};

  for (const filePath of files) {
    let text = "";
    try {
      text = readTextSafe(filePath);
    } catch {
      continue;
    }
    totalLines += text.split("\n").length;
    importCount += (text.match(/^\s*(import|from|require\()/gm) ?? []).length;
    controlFlowCount += (text.match(/\b(if|for|while|switch|case|catch|try)\b/g) ?? []).length;
    symbols += (text.match(/\b(class|def|function|const|let|var)\b/g) ?? []).length;
    const ext = path.extname(filePath).toLowerCase() || "<none>";
    languages[ext] = (languages[ext] ?? 0) + 1;
  }

  const recentFindingsByCategory: Record<string, number> = {};
  const retros = collectRetrospectivesForProject(projectPath);
  for (const retro of retros.slice(-5)) {
    const categories = retro.findings_by_category;
    if (!categories || typeof categories !== "object") continue;
    for (const [category, count] of Object.entries(categories as Record<string, unknown>)) {
      if (typeof count === "number") {
        recentFindingsByCategory[category] = (recentFindingsByCategory[category] ?? 0) + count;
      }
    }
  }

  const dominantLanguages = Object.entries(languages)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 5)
    .map(([language]) => language);

  const totalFindings = Object.values(recentFindingsByCategory).reduce((sum, value) => sum + value, 0);

  return {
    version: 1,
    target: projectPath,
    architecture_complexity_score: Number(((controlFlowCount + symbols) / Math.max(1, files.length)).toFixed(4)),
    dependency_flux: Number((importCount / Math.max(1, files.length)).toFixed(4)),
    finding_entropy: Number((totalFindings / Math.max(1, retros.slice(-5).length)).toFixed(4)),
    file_count: files.length,
    line_count: totalLines,
    import_count: importCount,
    control_flow_count: controlFlowCount,
    dominant_languages: dominantLanguages,
    recent_findings_by_category: recentFindingsByCategory,
  };
}

/**
 * Reconcile manifest stage with execution log.
 * If the log shows a later stage than the manifest, use the log's stage.
 * This handles cases where the skill forgot to update the manifest.
 */
function reconcileStage(taskDir: string, manifest: Record<string, unknown>): Record<string, unknown> {
  const stage = manifest.stage as string;
  if (stage === "DONE" || (typeof stage === "string" && stage.includes("FAIL"))) return manifest;

  try {
    const logPath = path.join(taskDir, "execution-log.md");
    const logContent = fs.readFileSync(logPath, "utf-8");
    if (logContent.includes("→ DONE") || logContent.includes("[ADVANCE] EXECUTION → DONE") || logContent.includes("[ADVANCE] AUDITING → DONE")) {
      return { ...manifest, stage: "DONE" };
    }
    // Check for last [STAGE] or [ADVANCE] line to get the real stage
    const stageLines = logContent.split("\n").filter((l: string) => l.includes("[STAGE]") || l.includes("[ADVANCE]"));
    if (stageLines.length > 0) {
      const last = stageLines[stageLines.length - 1];
      const match = last.match(/→\s*(\S+)/);
      if (match) {
        const logStage = match[1];
        // Only override if the log stage is "later" than manifest
        const STAGE_ORDER: Record<string, number> = {
          FOUNDRY_INITIALIZED: 0, DISCOVERY: 1, SPEC_NORMALIZATION: 2,
          SPEC_REVIEW: 3, PLANNING: 4, PLAN_REVIEW: 5, PLAN_AUDIT: 6,
          PRE_EXECUTION_SNAPSHOT: 7, EXECUTION: 8, TEST_EXECUTION: 9,
          CHECKPOINT_AUDIT: 10, AUDITING: 11, FINAL_AUDIT: 12, DONE: 13,
        };
        if ((STAGE_ORDER[logStage] ?? 0) > (STAGE_ORDER[stage] ?? 0)) {
          return { ...manifest, stage: logStage };
        }
      }
    }
  } catch {
    // No log or unreadable — keep manifest stage
  }
  return manifest;
}

function findRepoRoot(startDir: string): string {
  let dir = path.resolve(startDir);
  while (dir !== path.dirname(dir)) {
    // A repo root has both .dynos/ and .git/ (or at least .git)
    if (fs.existsSync(path.join(dir, ".dynos")) && fs.existsSync(path.join(dir, ".git"))) return dir;
    dir = path.dirname(dir);
  }
  // Fallback: walk again looking for .dynos only
  dir = path.resolve(startDir);
  while (dir !== path.dirname(dir)) {
    if (fs.existsSync(path.join(dir, ".dynos"))) return dir;
    dir = path.dirname(dir);
  }
  return path.resolve(startDir);
}

export function dynosApi(): Plugin {
  // Walk up from cwd to find the repo root (directory containing .dynos/)
  const repoRoot = findRepoRoot(process.cwd());
  const dynosctlPath = path.resolve(repoRoot, "hooks", "dynosctl.py");

  return {
    name: "dynos-api",
    configureServer(server) {
      server.middlewares.use((req: SimpleReq, res: SimpleRes, next: () => void) => {
        const rawUrl = req.url ?? "/";
        const method = (req.method ?? "GET").toUpperCase();

        // Only handle /api/ routes
        if (!rawUrl.startsWith("/api/")) {
          next();
          return;
        }

        const parsed = new URL(rawUrl, "http://localhost");
        const pathname = parsed.pathname;
        const projectParam = parsed.searchParams.get("project");
        const isGlobal = projectParam === "__global__";

        // Validate project param against registry whitelist
        let projectPath: string;
        if (projectParam && !isGlobal) {
          const decoded = decodeURIComponent(projectParam);
          if (!isRegisteredProject(decoded)) {
            jsonResponse(res, 400, { error: "Project not in registry" });
            return;
          }
          projectPath = path.resolve(decoded);
        } else {
          projectPath = repoRoot;
        }
        const slug = computeSlug(projectPath);

        // ---- GET routes ----
        if (method === "GET") {
          // GET /api/tasks
          if (pathname === "/api/tasks") {
            try {
              if (isGlobal) {
                const tasks = collectFromAllProjects<Record<string, unknown>>((pp) => {
                  const taskDirs = listTaskDirs(pp);
                  return taskDirs.map((td) => {
                    try {
                      const taskPath = path.join(localDynosDir(pp), td);
                      const manifest = readJsonFile(path.join(taskPath, "manifest.json")) as Record<string, unknown>;
                      return { ...reconcileStage(taskPath, manifest), task_dir: td, project_path: pp };
                    } catch {
                      return null;
                    }
                  }).filter(notNull);
                });
                jsonResponse(res, 200, tasks);
              } else {
                const taskDirs = listTaskDirs(projectPath);
                const tasks = taskDirs.map((td) => {
                  try {
                    const taskPath = path.join(localDynosDir(projectPath), td);
                    const manifest = readJsonFile(path.join(taskPath, "manifest.json")) as Record<string, unknown>;
                    return { ...reconcileStage(taskPath, manifest), task_dir: td, project_path: projectPath };
                  } catch {
                    return null;
                  }
                }).filter(notNull);
                jsonResponse(res, 200, tasks);
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/retrospective
          const retroMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/retrospective$/);
          if (retroMatch) {
            const taskId = retroMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const data = readJsonFile(path.join(localDynosDir(projectPath), taskId, "task-retrospective.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/execution-log
          const logMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/execution-log$/);
          if (logMatch) {
            const taskId = logMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const raw = readTextFile(path.join(localDynosDir(projectPath), taskId, "execution-log.md"));
              const lines = raw.split("\n").filter((l) => l.trim());
              jsonResponse(res, 200, { lines });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/execution-graph
          const graphMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/execution-graph$/);
          if (graphMatch) {
            const taskId = graphMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const data = readJsonFile(path.join(localDynosDir(projectPath), taskId, "execution-graph.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/agents (G)
          if (pathname === "/api/agents") {
            try {
              if (isGlobal) {
                const agents = collectFromAllProjects((pp, s) => {
                  try {
                    const data = readJsonFile(path.join(persistentDir(s), "learned-agents", "registry.json")) as { agents: unknown[] };
                    return (data.agents ?? []).map((a: unknown) => ({ ...(a as Record<string, unknown>), project_path: pp }));
                  } catch {
                    return [];
                  }
                });
                jsonResponse(res, 200, agents);
              } else {
                const data = readJsonFile(path.join(persistentDir(slug), "learned-agents", "registry.json")) as { agents: unknown[] };
                jsonResponse(res, 200, data.agents ?? []);
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/findings (G)
          if (pathname === "/api/findings") {
            try {
              if (isGlobal) {
                const findings = collectFromAllProjects((pp) => {
                  try {
                    const data = readJsonFile(path.join(localDynosDir(pp), "proactive-findings.json")) as { findings: unknown[] };
                    return (data.findings ?? []).map((f: unknown) => ({ ...(f as Record<string, unknown>), project_path: pp }));
                  } catch {
                    return [];
                  }
                });
                jsonResponse(res, 200, findings);
              } else {
                const data = readJsonFile(path.join(localDynosDir(projectPath), "proactive-findings.json")) as { findings: unknown[] };
                jsonResponse(res, 200, data.findings ?? []);
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/autofix-metrics (G)
          if (pathname === "/api/autofix-metrics") {
            try {
              if (isGlobal) {
                const allMetrics = collectFromAllProjects<Record<string, unknown>>((_, s) => {
                  try {
                    const data = readJsonFile(path.join(persistentDir(s), "autofix-metrics.json")) as Record<string, unknown>;
                    return [data];
                  } catch {
                    return [];
                  }
                });
                if (allMetrics.length === 0) {
                  jsonResponse(res, 200, { totals: {} });
                } else {
                  const merged: Record<string, number> = {};
                  for (const m of allMetrics) {
                    const totals = (m.totals ?? {}) as Record<string, number>;
                    for (const [key, val] of Object.entries(totals)) {
                      if (typeof val === "number") {
                        merged[key] = (merged[key] ?? 0) + val;
                      }
                    }
                  }
                  jsonResponse(res, 200, { totals: merged });
                }
              } else {
                const data = readJsonFile(path.join(persistentDir(slug), "autofix-metrics.json"));
                jsonResponse(res, 200, data);
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/policy
          if (pathname === "/api/policy") {
            try {
              const data = readJsonFile(path.join(persistentDir(slug), "policy.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/model-policy
          if (pathname === "/api/model-policy") {
            try {
              const data = readJsonFile(path.join(persistentDir(slug), "model-policy.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/route-policy
          if (pathname === "/api/route-policy") {
            try {
              const data = readJsonFile(path.join(persistentDir(slug), "route-policy.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/autofix-policy
          if (pathname === "/api/autofix-policy") {
            try {
              const data = readJsonFile(path.join(persistentDir(slug), "autofix-policy.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/skip-policy
          if (pathname === "/api/skip-policy") {
            try {
              const data = readJsonFile(path.join(persistentDir(slug), "skip-policy.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/registry
          if (pathname === "/api/registry") {
            try {
              const data = readJsonFile(path.join(homedir(), ".dynos", "registry.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/retrospectives (G)
          if (pathname === "/api/retrospectives") {
            try {
              if (isGlobal) {
                const retros = collectFromAllProjects<Record<string, unknown>>((pp) => {
                  const taskDirs = listTaskDirs(pp);
                  return taskDirs.map((td) => {
                    try {
                      const data = readJsonFile(path.join(localDynosDir(pp), td, "task-retrospective.json")) as Record<string, unknown>;
                      return { ...data, task_id: td, project_path: pp };
                    } catch {
                      return null;
                    }
                  }).filter(notNull);
                });
                jsonResponse(res, 200, retros);
              } else {
                const taskDirs = listTaskDirs(projectPath);
                const retros = taskDirs.map((td) => {
                  try {
                    const data = readJsonFile(path.join(localDynosDir(projectPath), td, "task-retrospective.json")) as Record<string, unknown>;
                    return { ...data, task_id: td, project_path: projectPath };
                  } catch {
                    return null;
                  }
                }).filter(notNull);
                jsonResponse(res, 200, retros);
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/report
          if (pathname === "/api/report") {
            try {
              if (isGlobal) {
                const reports = collectFromAllProjects<Record<string, unknown>>((pp) => {
                  try {
                    return [{ ...buildRepoReport(pp), project_path: pp }];
                  } catch {
                    return [];
                  }
                });
                const merged = reports.reduce<{
                  registry_updated_at: string | null;
                  summary: Record<string, number>;
                  active_routes: Record<string, unknown>[];
                  demotions: Record<string, unknown>[];
                  automation_queue: Record<string, unknown>[];
                  coverage_gaps: Record<string, unknown>[];
                  recent_runs: Record<string, unknown>[];
                }>((acc, report) => {
                  const summary = (report.summary as Record<string, number> | undefined) ?? {};
                  const accSummary = acc.summary;
                  for (const [key, value] of Object.entries(summary)) {
                    accSummary[key] = (accSummary[key] ?? 0) + (typeof value === "number" ? value : 0);
                  }
                  acc.active_routes.push(...(((report.active_routes as Record<string, unknown>[] | undefined) ?? []).map((item) => ({ ...item, project_path: report.project_path }))));
                  acc.demotions.push(...(((report.demotions as Record<string, unknown>[] | undefined) ?? []).map((item) => ({ ...item, project_path: report.project_path }))));
                  acc.automation_queue.push(...(((report.automation_queue as Record<string, unknown>[] | undefined) ?? []).map((item) => ({ ...item, project_path: report.project_path }))));
                  acc.coverage_gaps.push(...(((report.coverage_gaps as Record<string, unknown>[] | undefined) ?? []).map((item) => ({ ...item, project_path: report.project_path }))));
                  acc.recent_runs.push(...(((report.recent_runs as Record<string, unknown>[] | undefined) ?? []).map((item) => ({ ...item, project_path: report.project_path }))));
                  return acc;
                }, {
                  registry_updated_at: null,
                  summary: {
                    learned_components: 0,
                    active_routes: 0,
                    shadow_components: 0,
                    demoted_components: 0,
                    queued_automation_jobs: 0,
                    benchmark_runs: 0,
                    tracked_fixtures: 0,
                    coverage_gaps: 0,
                  },
                  active_routes: [] as Record<string, unknown>[],
                  demotions: [] as Record<string, unknown>[],
                  automation_queue: [] as Record<string, unknown>[],
                  coverage_gaps: [] as Record<string, unknown>[],
                  recent_runs: [] as Record<string, unknown>[],
                });
                merged.recent_runs = merged.recent_runs.slice(-10);
                jsonResponse(res, 200, merged);
              } else {
                jsonResponse(res, 200, buildRepoReport(projectPath));
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/project-stats
          if (pathname === "/api/project-stats") {
            try {
              if (isGlobal) {
                const statsList = collectFromAllProjects<Record<string, unknown>>((pp) => {
                  try {
                    return [buildProjectStats(pp)];
                  } catch {
                    return [];
                  }
                });
                const taskCountsByType: Record<string, number> = {};
                const executorReliabilityBuckets: Record<string, number[]> = {};
                const preventionRuleFrequencies: Record<string, number> = {};
                const preventionRuleExecutors: Record<string, string> = {};
                let totalTasks = 0;
                let qualityWeightedSum = 0;

                for (const stats of statsList) {
                  const statsTotalTasks = typeof stats.total_tasks === "number" ? stats.total_tasks : 0;
                  totalTasks += statsTotalTasks;
                  if (typeof stats.average_quality_score === "number") {
                    qualityWeightedSum += stats.average_quality_score * statsTotalTasks;
                  }
                  for (const [taskType, count] of Object.entries((stats.task_counts_by_type as Record<string, number> | undefined) ?? {})) {
                    taskCountsByType[taskType] = (taskCountsByType[taskType] ?? 0) + count;
                  }
                  for (const [role, reliability] of Object.entries((stats.executor_reliability as Record<string, number> | undefined) ?? {})) {
                    executorReliabilityBuckets[role] ??= [];
                    executorReliabilityBuckets[role].push(reliability);
                  }
                  for (const [rule, count] of Object.entries((stats.prevention_rule_frequencies as Record<string, number> | undefined) ?? {})) {
                    preventionRuleFrequencies[rule] = (preventionRuleFrequencies[rule] ?? 0) + count;
                  }
                  for (const [rule, executor] of Object.entries((stats.prevention_rule_executors as Record<string, string> | undefined) ?? {})) {
                    preventionRuleExecutors[rule] ??= executor;
                  }
                }

                const executorReliability = Object.fromEntries(
                  Object.entries(executorReliabilityBuckets).map(([role, values]) => [
                    role,
                    Number((values.reduce((sum, value) => sum + value, 0) / Math.max(1, values.length)).toFixed(3)),
                  ]),
                );

                jsonResponse(res, 200, {
                  total_tasks: totalTasks,
                  task_counts_by_type: taskCountsByType,
                  average_quality_score: Number((qualityWeightedSum / Math.max(1, totalTasks)).toFixed(3)),
                  executor_reliability: executorReliability,
                  prevention_rule_frequencies: preventionRuleFrequencies,
                  prevention_rule_executors: preventionRuleExecutors,
                });
              } else {
                jsonResponse(res, 200, buildProjectStats(projectPath));
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/state
          if (pathname === "/api/state") {
            try {
              if (isGlobal) {
                jsonResponse(res, 400, { error: "Repo state is only available for a single project" });
              } else {
                jsonResponse(res, 200, buildRepoState(projectPath));
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/manifest
          const manifestMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/manifest$/);
          if (manifestMatch) {
            const taskId = manifestMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const data = readJsonFile(path.join(localDynosDir(projectPath), taskId, "manifest.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/raw-input
          const rawInputMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/raw-input$/);
          if (rawInputMatch) {
            const taskId = rawInputMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const content = readTextFile(path.join(localDynosDir(projectPath), taskId, "raw-input.md"));
              jsonResponse(res, 200, { content });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/discovery-notes
          const discoveryMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/discovery-notes$/);
          if (discoveryMatch) {
            const taskId = discoveryMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const content = readTextFile(path.join(localDynosDir(projectPath), taskId, "discovery-notes.md"));
              jsonResponse(res, 200, { content });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/design-decisions
          const designMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/design-decisions$/);
          if (designMatch) {
            const taskId = designMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const content = readTextFile(path.join(localDynosDir(projectPath), taskId, "design-decisions.md"));
              jsonResponse(res, 200, { content });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/events
          const eventsMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/events$/);
          if (eventsMatch) {
            const taskId = eventsMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const raw = readTextFile(path.join(localDynosDir(projectPath), taskId, "events.jsonl"));
              const events = raw.split("\n").filter((l) => l.trim()).map((line) => {
                try { return JSON.parse(line); } catch { return null; }
              }).filter(notNull);
              jsonResponse(res, 200, { events });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/receipts
          const receiptsMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/receipts$/);
          if (receiptsMatch) {
            const taskId = receiptsMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const dirPath = path.join(localDynosDir(projectPath), taskId, "receipts");
              let entries: string[];
              try {
                entries = fs.readdirSync(dirPath).filter((e: string) => e.endsWith(".json"));
              } catch {
                jsonResponse(res, 200, { receipts: [] });
                return;
              }
              const receipts = entries.map((entry) => {
                try {
                  const data = readJsonFile(path.join(dirPath, entry));
                  return { filename: entry, data };
                } catch {
                  return null;
                }
              }).filter(notNull);
              jsonResponse(res, 200, { receipts });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/evidence
          const evidenceMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/evidence$/);
          if (evidenceMatch) {
            const taskId = evidenceMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const dirPath = path.join(localDynosDir(projectPath), taskId, "evidence");
              let entries: string[];
              try {
                entries = fs.readdirSync(dirPath).filter((e: string) => e.endsWith(".md"));
              } catch {
                jsonResponse(res, 200, { files: [] });
                return;
              }
              const files = entries.map((entry) => {
                try {
                  const content = readTextFile(path.join(dirPath, entry));
                  return { name: entry, content };
                } catch {
                  return null;
                }
              }).filter(notNull);
              jsonResponse(res, 200, { files });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/completion
          const completionMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/completion$/);
          if (completionMatch) {
            const taskId = completionMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const data = readJsonFile(path.join(localDynosDir(projectPath), taskId, "completion.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/postmortem
          const postmortemMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/postmortem$/);
          if (postmortemMatch) {
            const taskId = postmortemMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const pmDir = path.join(persistentDir(slug), "postmortems");
              const result: { json?: unknown; markdown?: string } = {};
              try {
                result.json = readJsonFile(path.join(pmDir, `${taskId}.json`));
              } catch { /* not found */ }
              try {
                result.markdown = readTextFile(path.join(pmDir, `${taskId}.md`));
              } catch { /* not found */ }
              if (result.json === undefined && result.markdown === undefined) {
                jsonResponse(res, 404, { error: "Not found" });
              } else {
                jsonResponse(res, 200, result);
              }
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/router-decisions
          const routerMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/router-decisions$/);
          if (routerMatch) {
            const taskId = routerMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const decisions: unknown[] = [];
              // Read task manifest for timestamp range
              let taskCreatedAt = "";
              let taskCompletedAt = "";
              try {
                const manifest = readJsonFile(path.join(localDynosDir(projectPath), taskId, "manifest.json")) as Record<string, unknown>;
                taskCreatedAt = (manifest.created_at as string) ?? "";
                taskCompletedAt = (manifest.completed_at as string) ?? "";
              } catch { /* no manifest */ }

              // Check per-task events first
              try {
                const raw = readTextFile(path.join(localDynosDir(projectPath), taskId, "events.jsonl"));
                const lines = raw.split("\n").filter((l) => l.trim());
                for (const line of lines) {
                  try {
                    const evt = JSON.parse(line) as Record<string, unknown>;
                    if (typeof evt.event === "string" && evt.event.startsWith("router_")) {
                      decisions.push(evt);
                    }
                  } catch { /* skip malformed lines */ }
                }
              } catch { /* no per-task events */ }
              // Also check global events — filter by task field OR timestamp range
              try {
                const globalPath = path.join(localDynosDir(projectPath), "events.jsonl");
                const raw = readTextFile(globalPath);
                const lines = raw.split("\n").filter((l) => l.trim());
                for (const line of lines) {
                  try {
                    const evt = JSON.parse(line) as Record<string, unknown>;
                    if (typeof evt.event !== "string" || !evt.event.startsWith("router_")) continue;
                    // Match by explicit task field
                    if (evt.task === taskId) {
                      decisions.push(evt);
                      continue;
                    }
                    // Match by timestamp range (router events lack task field)
                    if (taskCreatedAt && typeof evt.ts === "string") {
                      const evtTime = evt.ts;
                      const inRange = evtTime >= taskCreatedAt && (!taskCompletedAt || evtTime <= taskCompletedAt);
                      if (inRange) {
                        decisions.push(evt);
                      }
                    }
                  } catch { /* skip malformed lines */ }
                }
              } catch { /* no global events */ }
              jsonResponse(res, 200, { decisions });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/spec
          const specMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/spec$/);
          if (specMatch) {
            const taskId = specMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const content = readTextFile(path.join(localDynosDir(projectPath), taskId, "spec.md"));
              jsonResponse(res, 200, { content });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/plan
          const planMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/plan$/);
          if (planMatch) {
            const taskId = planMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const content = readTextFile(path.join(localDynosDir(projectPath), taskId, "plan.md"));
              jsonResponse(res, 200, { content });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/audit-reports
          const auditReportsMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/audit-reports$/);
          if (auditReportsMatch) {
            const taskId = auditReportsMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const dirPath = path.join(localDynosDir(projectPath), taskId, "audit-reports");
              let entries: string[];
              try {
                entries = fs.readdirSync(dirPath).filter((e: string) => e.endsWith(".json"));
              } catch {
                jsonResponse(res, 200, []);
                return;
              }
              const reports: unknown[] = [];
              for (const entry of entries) {
                try {
                  const data = readJsonFile(path.join(dirPath, entry)) as Record<string, unknown>;
                  // Normalize: ensure auditor_name exists (some auditors use report_id)
                  if (!data.auditor_name && data.report_id) {
                    const rid = String(data.report_id);
                    data.auditor_name = rid.replace(/-checkpoint$/, "").replace(/-vote-.*$/, "");
                  }
                  // Normalize: scope must be a string (some auditors write an object)
                  if (data.scope && typeof data.scope === "object") {
                    const scopeObj = data.scope as Record<string, unknown>;
                    data.scope = scopeObj.audit_start_sha
                      ? `${scopeObj.audit_start_sha} (${Array.isArray(scopeObj.files_audited) ? (scopeObj.files_audited as string[]).length + " files" : "unknown"})`
                      : "changed-files";
                  }
                  if (!data.scope) {
                    data.scope = "changed-files";
                  }
                  reports.push(data);
                } catch {
                  // skip malformed files
                }
              }
              jsonResponse(res, 200, reports);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/tasks/:taskId/token-usage
          const tokenUsageMatch = pathname.match(/^\/api\/tasks\/([^/]+)\/token-usage$/);
          if (tokenUsageMatch) {
            const taskId = tokenUsageMatch[1];
            if (!TASK_ID_PATTERN.test(taskId)) {
              jsonResponse(res, 400, { error: "Invalid task ID" });
              return;
            }
            try {
              const data = readJsonFile(path.join(localDynosDir(projectPath), taskId, "token-usage.json"));
              jsonResponse(res, 200, data);
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/cost-summary
          if (pathname === "/api/cost-summary") {
            try {
              const RATES_PER_MILLION: Record<string, { input: number; output: number }> = {
                haiku: { input: 0.80, output: 4.00 },
                sonnet: { input: 3.00, output: 15.00 },
                opus: { input: 15.00, output: 75.00 },
              };
              const byModel: Record<string, { input_tokens: number; output_tokens: number; tokens: number; estimated_usd: number }> = {};
              const byAgent: Record<string, { input_tokens: number; output_tokens: number; tokens: number }> = {};
              let totalTokens = 0;
              let totalInputTokens = 0;
              let totalOutputTokens = 0;
              let totalUsd = 0;

              const taskDirs = listTaskDirs(projectPath);
              for (const td of taskDirs) {
                let usage: Record<string, unknown>;
                try {
                  usage = readJsonFile(path.join(localDynosDir(projectPath), td, "token-usage.json")) as Record<string, unknown>;
                } catch {
                  continue;
                }

                // Aggregate by model
                const models = (usage.by_model ?? {}) as Record<string, Record<string, unknown>>;
                for (const [model, info] of Object.entries(models)) {
                  const inputTok = typeof info.input_tokens === "number" ? info.input_tokens : 0;
                  const outputTok = typeof info.output_tokens === "number" ? info.output_tokens : 0;
                  const tokens = typeof info.tokens === "number" ? info.tokens : (inputTok + outputTok);
                  const key = model.toLowerCase();
                  if (!byModel[key]) {
                    byModel[key] = { input_tokens: 0, output_tokens: 0, tokens: 0, estimated_usd: 0 };
                  }
                  byModel[key].input_tokens += inputTok;
                  byModel[key].output_tokens += outputTok;
                  byModel[key].tokens += tokens;
                  const rates = RATES_PER_MILLION[key] ?? { input: 3.00, output: 15.00 };
                  const cost = (inputTok / 1_000_000) * rates.input + (outputTok / 1_000_000) * rates.output;
                  byModel[key].estimated_usd += cost;
                  totalTokens += tokens;
                  totalInputTokens += inputTok;
                  totalOutputTokens += outputTok;
                  totalUsd += cost;
                }

                // Aggregate by agent
                const agents = (usage.by_agent ?? {}) as Record<string, Record<string, unknown>>;
                for (const [agent, info] of Object.entries(agents)) {
                  const inputTok = typeof info.input_tokens === "number" ? info.input_tokens : 0;
                  const outputTok = typeof info.output_tokens === "number" ? info.output_tokens : 0;
                  const tokens = typeof info.tokens === "number" ? info.tokens : (inputTok + outputTok);
                  if (!byAgent[agent]) {
                    byAgent[agent] = { input_tokens: 0, output_tokens: 0, tokens: 0 };
                  }
                  byAgent[agent].input_tokens += inputTok;
                  byAgent[agent].output_tokens += outputTok;
                  byAgent[agent].tokens += tokens;
                }
              }

              jsonResponse(res, 200, {
                by_model: byModel,
                by_agent: byAgent,
                total_tokens: totalTokens,
                total_input_tokens: totalInputTokens,
                total_output_tokens: totalOutputTokens,
                total_estimated_usd: Math.round(totalUsd * 100) / 100,
              });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }

          // GET /api/maintainer-status
          if (pathname === "/api/maintainer-status") {
            const statusPath = path.join(localDynosDir(projectPath), "maintenance", "status.json");
            const data = readJsonFileOrDefault(statusPath, { running: false });
            jsonResponse(res, 200, data);
            return;
          }

          // GET /api/maintenance-cycles
          if (pathname === "/api/maintenance-cycles") {
            const lastParam = parsed.searchParams.get("last");
            const lastN = lastParam ? parseInt(lastParam, 10) : 20;
            const cyclesPath = path.join(localDynosDir(projectPath), "maintenance", "cycles.jsonl");
            try {
              const raw = readTextFile(cyclesPath);
              const lines = raw.split("\n").filter((l: string) => l.trim());
              const allCycles: unknown[] = [];
              for (const line of lines) {
                try {
                  allCycles.push(JSON.parse(line));
                } catch {
                  // skip malformed line
                }
              }
              const sliced = allCycles.slice(-lastN);
              jsonResponse(res, 200, { total_cycles: allCycles.length, cycles: sliced });
            } catch {
              jsonResponse(res, 200, { total_cycles: 0, cycles: [] });
            }
            return;
          }

          // GET /api/control-plane
          if (pathname === "/api/control-plane") {
            try {
              const maintainer = readJsonFileOrDefault(
                path.join(localDynosDir(projectPath), "maintenance", "status.json"),
                { running: false } as Record<string, unknown>,
              );

              const autofixEnabled = fs.existsSync(
                path.join(localDynosDir(projectPath), "maintenance", "autofix.enabled"),
              );

              const queue = readJsonFileOrDefault(
                path.join(localDynosDir(projectPath), "automation", "queue.json"),
                { version: 0, updated_at: "", items: [] } as Record<string, unknown>,
              );

              const automationStatus = readJsonFileOrDefault(
                path.join(localDynosDir(projectPath), "automation", "status.json"),
                { updated_at: "", queued_before: 0, executed: 0, pending_after: 0 } as Record<string, unknown>,
              );

              const pDir = persistentDir(slug);
              const registry = readJsonFileOrDefault(
                path.join(pDir, "learned-agents", "registry.json"),
                { agents: [] } as Record<string, unknown>,
              );
              const agents = (Array.isArray((registry as Record<string, unknown>).agents)
                ? (registry as Record<string, unknown>).agents
                : []) as Array<Record<string, unknown>>;

              const history = readJsonFileOrDefault(
                path.join(pDir, "benchmarks", "history.json"),
                { runs: [] } as Record<string, unknown>,
              );
              const allRuns = (Array.isArray((history as Record<string, unknown>).runs)
                ? (history as Record<string, unknown>).runs
                : []) as Array<Record<string, unknown>>;
              const recentRuns = allRuns.slice(-10);

              const benchIndex = readJsonFileOrDefault(
                path.join(pDir, "benchmarks", "index.json"),
                { fixtures: [] } as Record<string, unknown>,
              );
              const fixtures = (Array.isArray((benchIndex as Record<string, unknown>).fixtures)
                ? (benchIndex as Record<string, unknown>).fixtures
                : []) as Array<Record<string, unknown>>;

              // Compute agent_summary
              const agentSummary = {
                total: agents.length,
                routeable: agents.filter((a) => a.route_allowed).length,
                shadow: agents.filter((a) => a.mode === "shadow").length,
                alongside: agents.filter((a) => a.mode === "alongside").length,
                replace: agents.filter((a) => a.mode === "replace").length,
                demoted: agents.filter((a) => a.mode === "demoted").length,
              };

              // Compute freshness buckets
              const bucketMap: Record<string, string[]> = {
                Fresh: [],
                Recent: [],
                Aging: [],
                Stale: [],
                Unbenchmarked: [],
              };
              for (const agent of agents) {
                const bs = agent.benchmark_summary as Record<string, unknown> | undefined;
                if (!bs || (typeof bs.sample_count === "number" && bs.sample_count === 0)) {
                  bucketMap.Unbenchmarked.push(agent.agent_name as string);
                } else {
                  const offset = typeof agent.last_benchmarked_task_offset === "number"
                    ? agent.last_benchmarked_task_offset
                    : 999;
                  if (offset === 0) {
                    bucketMap.Fresh.push(agent.agent_name as string);
                  } else if (offset <= 2) {
                    bucketMap.Recent.push(agent.agent_name as string);
                  } else if (offset <= 5) {
                    bucketMap.Aging.push(agent.agent_name as string);
                  } else {
                    bucketMap.Stale.push(agent.agent_name as string);
                  }
                }
              }
              const freshnessBuckets = Object.entries(bucketMap)
                .filter(([, arr]) => arr.length > 0)
                .map(([label, arr]) => ({ label, count: arr.length, agents: arr }));

              // Compute coverage gaps
              const agentNames = new Set(agents.map((a) => a.agent_name as string));
              const coverageGaps = fixtures
                .filter((f) => !agentNames.has(f.target_name as string))
                .map((f) => ({
                  target_name: f.target_name as string,
                  role: f.role as string,
                  task_type: f.task_type as string,
                  item_kind: f.item_kind as string,
                }));

              // Compute attention items
              const urgencyOrder = ["demoted on regression", "unbenchmarked", "stale benchmark", "coverage gap"];
              const attentionItems: Array<Record<string, unknown>> = [];

              for (const agent of agents) {
                if (agent.mode === "demoted") {
                  const lastEval = agent.last_evaluation as Record<string, unknown> | undefined;
                  attentionItems.push({
                    agent_name: agent.agent_name,
                    reason: "demoted on regression",
                    mode: agent.mode,
                    status: agent.status,
                    recommendation: lastEval?.recommendation ?? null,
                    delta_composite: lastEval?.delta_composite ?? null,
                  });
                }
              }

              for (const agent of agents) {
                const bs = agent.benchmark_summary as Record<string, unknown> | undefined;
                if (!bs || (typeof bs.sample_count === "number" && bs.sample_count === 0)) {
                  if (agent.mode !== "demoted") {
                    attentionItems.push({
                      agent_name: agent.agent_name,
                      reason: "unbenchmarked",
                      mode: agent.mode,
                      status: agent.status,
                      recommendation: null,
                      delta_composite: null,
                    });
                  }
                }
              }

              for (const agent of agents) {
                const bs = agent.benchmark_summary as Record<string, unknown> | undefined;
                const isBenchmarked = bs && typeof bs.sample_count === "number" && bs.sample_count > 0;
                const offset = typeof agent.last_benchmarked_task_offset === "number"
                  ? agent.last_benchmarked_task_offset
                  : 0;
                if (isBenchmarked && offset > 5 && agent.mode !== "demoted") {
                  attentionItems.push({
                    agent_name: agent.agent_name,
                    reason: "stale benchmark",
                    mode: agent.mode,
                    status: agent.status,
                    recommendation: null,
                    delta_composite: null,
                  });
                }
              }

              for (const gap of coverageGaps) {
                attentionItems.push({
                  agent_name: gap.target_name,
                  reason: "coverage gap",
                  mode: "",
                  status: "",
                  recommendation: null,
                  delta_composite: null,
                });
              }

              attentionItems.sort(
                (a, b) => urgencyOrder.indexOf(a.reason as string) - urgencyOrder.indexOf(b.reason as string),
              );

              jsonResponse(res, 200, {
                maintainer,
                autofix_enabled: autofixEnabled,
                queue,
                automation_status: automationStatus,
                agents,
                freshness_buckets: freshnessBuckets,
                coverage_gaps: coverageGaps,
                attention_items: attentionItems,
                recent_runs: recentRuns,
                agent_summary: agentSummary,
              });
            } catch (err) {
              handleFsError(res, err);
            }
            return;
          }
        }

        // ---- POST routes ----
        if (method === "POST") {
          // Block global writes
          if (isGlobal) {
            jsonResponse(res, 400, { error: "Global mode not supported for this endpoint" });
            return;
          }

          // POST /api/policy
          if (pathname === "/api/policy") {
            parseBody(req).then((body) => {
              try {
                atomicWriteJson(path.join(persistentDir(slug), "policy.json"), body);
                jsonResponse(res, 200, { ok: true });
              } catch (err) {
                handleFsError(res, err);
              }
            }).catch(() => {
              jsonResponse(res, 400, { error: "Invalid JSON body" });
            });
            return;
          }

          // POST /api/autofix-policy
          if (pathname === "/api/autofix-policy") {
            parseBody(req).then((body) => {
              try {
                atomicWriteJson(path.join(persistentDir(slug), "autofix-policy.json"), body);
                jsonResponse(res, 200, { ok: true });
              } catch (err) {
                handleFsError(res, err);
              }
            }).catch(() => {
              jsonResponse(res, 400, { error: "Invalid JSON body" });
            });
            return;
          }

          // POST /api/daemon/:action
          const daemonMatch = pathname.match(/^\/api\/daemon\/([^/]+)$/);
          if (daemonMatch) {
            const action = daemonMatch[1];
            let command: string;

            if (action === "status") {
              command = `python3 "${dynosctlPath}" active-task --root .`;
            } else if (action === "validate") {
              parseBody(req).then((body) => {
                const taskDir = (body as Record<string, string>)?.taskDir;
                if (!taskDir || !TASK_ID_PATTERN.test(taskDir)) {
                  jsonResponse(res, 400, { error: "Invalid or missing taskDir" });
                  return;
                }
                const cmd = `python3 "${dynosctlPath}" validate-task ${taskDir}`;
                exec(cmd, { cwd: projectPath, timeout: 30000, maxBuffer: 1024 * 1024 }, (err, stdout, stderr) => {
                  if (err) {
                    jsonResponse(res, 500, { ok: false, error: err.message, stdout: stdout ?? "", stderr: stderr ?? "" });
                    return;
                  }
                  jsonResponse(res, 200, { ok: true, stdout, stderr });
                });
              }).catch(() => {
                jsonResponse(res, 400, { error: "Invalid JSON body" });
              });
              return;
            } else {
              jsonResponse(res, 400, { error: `Unknown daemon action: ${action}` });
              return;
            }

            exec(command, { cwd: projectPath, timeout: 30000, maxBuffer: 1024 * 1024 }, (err, stdout, stderr) => {
              if (err) {
                jsonResponse(res, 500, { ok: false, error: err.message, stdout: stdout ?? "", stderr: stderr ?? "" });
                return;
              }
              jsonResponse(res, 200, { ok: true, stdout, stderr });
            });
            return;
          }
        }

        // Unknown /api/ route - pass through
        next();
      });
    },
  };
}

function handleFsError(
  res: { statusCode: number; setHeader: (k: string, v: string) => void; end: (body: string) => void },
  err: unknown,
): void {
  if (err && typeof err === "object" && "code" in err && (err as NodeJS.ErrnoException).code === "ENOENT") {
    jsonResponse(res, 404, { error: "Not found" });
  } else if (err instanceof SyntaxError) {
    jsonResponse(res, 500, { error: "Invalid JSON in file" });
  } else {
    jsonResponse(res, 500, { error: "Internal server error" });
  }
}

export default dynosApi;
