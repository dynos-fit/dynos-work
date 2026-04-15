/**
 * Pure capability-aware rendering helpers for dynos-work template tokens.
 *
 * Each helper reads `config.capabilities.*` and returns a string. No I/O except
 * `renderAgentSpawnBlock` in the degraded branch, which reads the agent body
 * file from disk so it can be inlined into the caller's skill body.
 *
 * Acceptance criteria satisfied:
 *   - C13: five pure helpers (renderAgentSpawnBlock, renderHooksPath,
 *     renderModel, renderAskUserBlock, renderSessionStartBootstrap)
 *   - C14: renderAgentSpawnBlock accepts {agent, phase?, instruction?} args and
 *     is the dispatch target for the balanced-brace {{SPAWN:{json}}} pre-pass
 *     in template.ts.
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

import type { PlatformConfig } from '../types/index.ts';

const __dirname = dirname(fileURLToPath(import.meta.url));
// After bun build: dist/index.js -> ../assets = cli/assets.
// In dev (bun test src/...): src/utils -> ../../assets = cli/assets.
const ASSETS_DIR_BUILT = join(__dirname, '..', 'assets');
const ASSETS_DIR_DEV = join(__dirname, '..', '..', 'assets');

function resolveAssetsDir(): string {
  // Prefer the built layout; fall back to dev layout if assets not found.
  try {
    // Using a cheap existence probe via statSync would require another import;
    // readdirSync would throw if missing. Use a sentinel file read instead.
    readFileSync(join(ASSETS_DIR_BUILT, 'templates', 'platforms', 'claude.json'), 'utf-8');
    return ASSETS_DIR_BUILT;
  } catch {
    return ASSETS_DIR_DEV;
  }
}

const ASSETS_DIR = resolveAssetsDir();

/** Arguments accepted by the `{{SPAWN:{json}}}` placeholder and by renderAgentSpawnBlock. */
export interface SpawnArgs {
  agent: string;
  phase?: string;
  instruction?: string;
}

/**
 * C13 helper #1 — render the hooks path segment.
 *
 * Claude (env_injection=true) — use the `${CLAUDE_PLUGIN_ROOT}/hooks` literal
 * env-var expansion (resolved by the Claude harness at runtime).
 * Non-Claude (env_injection=false) — use the absolute `~/.dynos-work/hooks`
 * path, since other harnesses do not set `CLAUDE_PLUGIN_ROOT` / `PLUGIN_HOOKS`.
 */
export function renderHooksPath(config: PlatformConfig, _isGlobal: boolean): string {
  if (!config || !config.capabilities) {
    throw new Error('renderHooksPath: config.capabilities is required');
  }
  if (config.capabilities.env_injection) {
    return '${CLAUDE_PLUGIN_ROOT}/hooks';
  }
  return '~/.dynos-work/hooks';
}

/**
 * C13 helper #2 — render a model value.
 *
 * Claude (per_agent_model=true) — pass through the literal model name so the
 * harness's `model:` frontmatter is respected.
 * Non-Claude — emit a markdown/HTML comment so that no YAML parser treats the
 * hint as a harness-recognised model field.
 */
export function renderModel(config: PlatformConfig, model: string): string {
  if (!config || !config.capabilities) {
    throw new Error('renderModel: config.capabilities is required');
  }
  if (typeof model !== 'string' || model.length === 0) {
    throw new Error('renderModel: model must be a non-empty string');
  }
  if (config.capabilities.per_agent_model) {
    return model;
  }
  return `<!-- model hint: ${model} (harness controls model selection) -->`;
}

/**
 * Locate the tokenized agent body file for an agent name. Raises if missing.
 */
function readAgentBody(agent: string): string {
  const safe = agent.replace(/[^a-zA-Z0-9._-]/g, '');
  if (!safe || safe !== agent) {
    throw new Error(`renderAgentSpawnBlock: invalid agent name: ${agent}`);
  }
  const agentPath = join(ASSETS_DIR, 'templates', 'base', 'agents', `${safe}.md`);
  let content: string;
  try {
    content = readFileSync(agentPath, 'utf-8');
  } catch (err) {
    throw new Error(
      `renderAgentSpawnBlock: cannot read agent body ${agentPath}: ${(err as Error).message}`,
    );
  }
  // Strip leading frontmatter block if present.
  if (content.startsWith('---\n')) {
    const end = content.indexOf('\n---', 4);
    if (end !== -1) {
      // Skip past the closing --- and any following newline.
      let cut = end + '\n---'.length;
      if (content[cut] === '\n') cut += 1;
      content = content.slice(cut);
    }
  }
  return content;
}

/**
 * C13 helper #3 + C14 dispatch target — render an agent spawn block.
 *
 * Claude (parallel_subagents=true) — emit the idiomatic "Spawn the ... subagent
 * (`dynos-work:<agent>`) with instruction:" paragraph followed by a fenced
 * code block carrying the instruction (or a generic description if absent).
 *
 * Non-Claude — inline the agent body under an `### Role:` heading so a single
 * harness turn executes what would otherwise have been a subagent.
 */
export function renderAgentSpawnBlock(config: PlatformConfig, args: SpawnArgs): string {
  if (!config || !config.capabilities) {
    throw new Error('renderAgentSpawnBlock: config.capabilities is required');
  }
  if (!args || typeof args !== 'object') {
    throw new Error('renderAgentSpawnBlock: args object is required');
  }
  if (typeof args.agent !== 'string' || args.agent.length === 0) {
    throw new Error('renderAgentSpawnBlock: args.agent is required');
  }

  const agent = args.agent;
  const phase = typeof args.phase === 'string' && args.phase.length > 0 ? args.phase : undefined;
  const instruction =
    typeof args.instruction === 'string' && args.instruction.length > 0
      ? args.instruction
      : `Perform the ${agent} role${phase ? ` for phase: ${phase}` : ''}.`;

  if (config.capabilities.parallel_subagents) {
    const phasePart = phase ? ` (phase: ${phase})` : '';
    return `Spawn the ${agent} subagent (\`dynos-work:${agent}\`)${phasePart} with instruction:\n\n\`\`\`text\n${instruction}\n\`\`\``;
  }

  // Degraded branch: inline the agent body.
  const body = readAgentBody(agent);
  const header = `### Role: ${agent}${phase ? ` — phase: ${phase}` : ''} — perform the following inline\n\n`;
  const instructionBlock = `Instruction:\n\n\`\`\`text\n${instruction}\n\`\`\`\n\n`;
  return header + instructionBlock + body;
}

/**
 * C13 helper #4 — render the ask-user block.
 *
 * Claude (structured_questions=true) — emit a line instructing the harness to
 * use the native `AskUserQuestion` tool.
 * Non-Claude — render a plain numbered list of questions with their options,
 * suitable for any text-based harness turn.
 */
export function renderAskUserBlock(config: PlatformConfig, questions?: unknown): string {
  if (!config || !config.capabilities) {
    throw new Error('renderAskUserBlock: config.capabilities is required');
  }
  if (config.capabilities.structured_questions) {
    return 'Present the questions to the user using `AskUserQuestion`.';
  }
  const lines: string[] = [
    'Present the following questions to the user and wait for their response:',
    '',
  ];
  if (Array.isArray(questions)) {
    questions.forEach((q, idx) => {
      if (q && typeof q === 'object') {
        const question = (q as { question?: unknown }).question;
        const options = (q as { options?: unknown }).options;
        if (typeof question === 'string') {
          lines.push(`${idx + 1}. ${question}`);
          if (Array.isArray(options) && options.length > 0) {
            const opts = options
              .filter((o): o is string => typeof o === 'string')
              .map((o) => `\`${o}\``)
              .join(', ');
            if (opts.length > 0) {
              lines.push(`   Options: ${opts}`);
            }
          }
        }
      }
    });
  }
  return lines.join('\n');
}

/**
 * C13 helper #5 — render the SessionStart bootstrap paragraph.
 *
 * Claude (lifecycle_hooks=true) — emit the exact hook-aware paragraph that the
 * fixture uses (uses `${PLUGIN_HOOKS}` — the env var injected by the Claude
 * plugin runtime).
 * Non-Claude — same semantic behaviour (registry + maintenance daemon) but
 * with the hook path resolved to an absolute `~/.dynos-work/hooks` path since
 * other harnesses do not export the `PLUGIN_HOOKS` env var.
 */
export function renderSessionStartBootstrap(config: PlatformConfig): string {
  if (!config || !config.capabilities) {
    throw new Error('renderSessionStartBootstrap: config.capabilities is required');
  }
  const hookPath = config.capabilities.lifecycle_hooks
    ? '${PLUGIN_HOOKS}'
    : '~/.dynos-work/hooks';
  return (
    'Ensure `.dynos/` exists: `mkdir -p .dynos`. ' +
    'Then auto-register this project with the global registry (silent, idempotent): run ' +
    `\`python3 "${hookPath}/dynoregistry.py" register "$(pwd)" 2>/dev/null || true\`. ` +
    'This creates `~/.dynos/projects/{slug}/` and adds the project to `~/.dynos/registry.json` if not already registered. ' +
    'No user action needed. Then ensure the local maintenance daemon is running (silent, idempotent): run ' +
    `\`PYTHONPATH="${hookPath}:\${PYTHONPATH:-}" python3 "${hookPath}/dynomaintain.py" start --root "$(pwd)" 2>/dev/null || true\`. ` +
    'This starts the daemon without autofix. Autofix must be explicitly enabled by the user via `dynos init --autofix` or `dynos local start --autofix`. ' +
    'If already running, it is a no-op.'
  );
}
