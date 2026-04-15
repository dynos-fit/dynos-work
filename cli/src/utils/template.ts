/**
 * Template renderer for dynos-work-cli.
 *
 * Ports the normative behavior from the ui-ux-pro-max reference implementation
 * and extends it with dynos-work-specific logic:
 *   - Iterate the 22 base skill bodies (readdir base/*.md)
 *   - Walk `config.extraFiles` and emit them (with placeholder substitution)
 *   - Apply `config.skillNamePrefix` for flat layouts (copilot)
 *   - Copy `{skill}.extra/` dirs into the emitted skill directory
 *   - Two-pass placeholder substitution with balanced-brace {{SPAWN:{json}}}
 *     pre-pass (C14) followed by bare-token replacement
 *   - Agent rendering: Claude gets standalone agent files with literal `model:`;
 *     non-Claude harnesses get agent bodies inlined by renderAgentSpawnBlock.
 *
 * Acceptance criteria satisfied:
 *   - C12: exports loadPlatformConfig, renderSkillFile, generatePlatformFiles,
 *     generateAllPlatformFiles, AI_TO_PLATFORM
 *   - C14: renderSpawnPlaceholders balanced-brace JSON extraction
 */
import { readFile, writeFile, mkdir, access, readdir, cp } from 'node:fs/promises';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { homedir } from 'node:os';

import type { PlatformConfig } from '../types/index.ts';
import {
  renderAgentSpawnBlock,
  renderAskUserBlock,
  renderHooksPath,
  renderModel,
  renderSessionStartBootstrap,
  type SpawnArgs,
} from './render-blocks.ts';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ASSETS_DIR_BUILT = join(__dirname, '..', 'assets');
const ASSETS_DIR_DEV = join(__dirname, '..', '..', 'assets');

function resolveAssetsDir(): string {
  try {
    readFileSync(join(ASSETS_DIR_BUILT, 'templates', 'platforms', 'claude.json'), 'utf-8');
    return ASSETS_DIR_BUILT;
  } catch {
    return ASSETS_DIR_DEV;
  }
}

const ASSETS_DIR = resolveAssetsDir();

/**
 * C12: AI type → platform config file name.
 * Note the `antigravity → agent` asymmetry (D9): the harness is called
 * antigravity but its platform config file is `agent.json`.
 */
export const AI_TO_PLATFORM: Record<string, string> = {
  claude: 'claude',
  cursor: 'cursor',
  windsurf: 'windsurf',
  antigravity: 'agent',
  copilot: 'copilot',
  kiro: 'kiro',
  opencode: 'opencode',
  roocode: 'roocode',
  codex: 'codex',
  qoder: 'qoder',
  gemini: 'gemini',
  trae: 'trae',
  continue: 'continue',
  codebuddy: 'codebuddy',
  droid: 'droid',
  kilocode: 'kilocode',
  warp: 'warp',
  augment: 'augment',
};

/**
 * Model hints used by the Claude harness. Derived from the canonical frontmatter
 * values in the claude-parity fixture. All 17 agents carry a `model:` key.
 */
const AGENT_MODELS: Record<string, string> = {
  'backend-executor': 'opus',
  'code-quality-auditor': 'sonnet',
  'db-executor': 'opus',
  'db-schema-auditor': 'opus',
  'dead-code-auditor': 'sonnet',
  'integration-executor': 'opus',
  investigator: 'opus',
  'ml-executor': 'opus',
  planning: 'opus',
  'refactor-executor': 'opus',
  'repair-coordinator': 'sonnet',
  'security-auditor': 'opus',
  'spec-completion-auditor': 'sonnet',
  'state-encoder': 'sonnet',
  'testing-executor': 'opus',
  'ui-auditor': 'sonnet',
  'ui-executor': 'opus',
};

async function exists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

/** C12: Load a platform config JSON by AIType. */
export async function loadPlatformConfig(aiType: string): Promise<PlatformConfig> {
  const platformName = AI_TO_PLATFORM[aiType];
  if (!platformName) {
    throw new Error(`Unknown AI type: ${aiType}`);
  }
  const configPath = join(ASSETS_DIR, 'templates', 'platforms', `${platformName}.json`);
  let content: string;
  try {
    content = await readFile(configPath, 'utf-8');
  } catch (err) {
    throw new Error(
      `Failed to read platform config for ${aiType} at ${configPath}: ${(err as Error).message}`,
    );
  }
  try {
    return JSON.parse(content) as PlatformConfig;
  } catch (err) {
    throw new Error(
      `Invalid JSON in platform config ${configPath}: ${(err as Error).message}`,
    );
  }
}

/**
 * C14 — Pre-pass that replaces argumented `{{SPAWN:{json}}}` placeholders with
 * the output of `renderAgentSpawnBlock(config, args)`.
 *
 * Uses balanced-brace matching (NOT a naive `\{[^}]+\}` regex) so that JSON
 * arguments containing nested braces — e.g. `{"meta":{"k":"v"}}` — parse
 * correctly. The scanner locates the `{{SPAWN:` literal, counts `{`/`}`
 * balance starting from the byte after the colon, stops once balance hits
 * zero, then consumes the trailing `}}`.
 */
export function renderSpawnPlaceholders(body: string, config: PlatformConfig): string {
  if (typeof body !== 'string') return body as unknown as string;
  const marker = '{{SPAWN:';
  let out = '';
  let cursor = 0;

  while (cursor < body.length) {
    const start = body.indexOf(marker, cursor);
    if (start === -1) {
      out += body.slice(cursor);
      break;
    }
    out += body.slice(cursor, start);
    // JSON payload starts at index `start + marker.length`.
    const jsonStart = start + marker.length;
    if (body[jsonStart] !== '{') {
      throw new Error(
        `renderSpawnPlaceholders: malformed {{SPAWN:...}} at offset ${start} — expected JSON object`,
      );
    }
    // Balanced-brace scan. Track string-literal state so braces inside JSON
    // strings are not counted.
    let depth = 0;
    let i = jsonStart;
    let inString = false;
    let escape = false;
    for (; i < body.length; i++) {
      const ch = body[i];
      if (inString) {
        if (escape) {
          escape = false;
        } else if (ch === '\\') {
          escape = true;
        } else if (ch === '"') {
          inString = false;
        }
        continue;
      }
      if (ch === '"') {
        inString = true;
        continue;
      }
      if (ch === '{') depth += 1;
      else if (ch === '}') {
        depth -= 1;
        if (depth === 0) {
          i += 1; // consume the closing brace
          break;
        }
      }
    }
    if (depth !== 0) {
      throw new Error(
        `renderSpawnPlaceholders: unbalanced braces in {{SPAWN:...}} starting at offset ${start}`,
      );
    }
    const jsonPayload = body.slice(jsonStart, i);
    // After the JSON payload we must see the trailing `}}` that closes the
    // placeholder.
    if (body.slice(i, i + 2) !== '}}') {
      throw new Error(
        `renderSpawnPlaceholders: expected "}}" after JSON payload at offset ${i}`,
      );
    }
    const end = i + 2;
    let args: SpawnArgs;
    try {
      args = JSON.parse(jsonPayload) as SpawnArgs;
    } catch (err) {
      throw new Error(
        `renderSpawnPlaceholders: invalid JSON in {{SPAWN:...}} at offset ${start}: ${(err as Error).message} — payload: ${jsonPayload}`,
      );
    }
    out += renderAgentSpawnBlock(config, args);
    cursor = end;
  }
  return out;
}

/** Apply bare-token placeholder substitution (pass 2). */
function applyBareTokens(body: string, config: PlatformConfig, isGlobal: boolean): string {
  const hooksPath = renderHooksPath(config, isGlobal);
  const askUserBlock = renderAskUserBlock(config);
  const bootstrap = renderSessionStartBootstrap(config);
  return body
    .replace(/\{\{HOOKS_PATH\}\}/g, hooksPath)
    .replace(/\{\{ASK_USER_BLOCK\}\}/g, askUserBlock)
    .replace(/\{\{SESSION_START_BOOTSTRAP\}\}/g, bootstrap);
}

/**
 * C12 — Render a single skill body file.
 *
 * Two-pass substitution:
 *   Pass 1 — argumented `{{SPAWN:{json}}}` via renderSpawnPlaceholders.
 *   Pass 2 — bare `{{HOOKS_PATH}}`, `{{ASK_USER_BLOCK}}`,
 *            `{{SESSION_START_BOOTSTRAP}}`.
 *
 * `{{MODEL}}` is intentionally NOT substituted here — it only appears in agent
 * frontmatter, which is rendered by the agent emission path (Claude only).
 */
export async function renderSkillFile(
  config: PlatformConfig,
  skillName: string,
  isGlobal = false,
): Promise<string> {
  if (!skillName || skillName.includes('/') || skillName.includes('\\')) {
    throw new Error(`renderSkillFile: invalid skill name: ${skillName}`);
  }
  const skillPath = join(ASSETS_DIR, 'templates', 'base', `${skillName}.md`);
  let raw: string;
  try {
    raw = await readFile(skillPath, 'utf-8');
  } catch (err) {
    throw new Error(
      `renderSkillFile: cannot read skill body ${skillPath}: ${(err as Error).message}`,
    );
  }

  // Base skill files already carry their own frontmatter. For harnesses whose
  // `config.frontmatter === null` (e.g. warp), strip the leading frontmatter
  // block so we don't emit a `---` block the harness will try to parse. For
  // all other harnesses, the existing frontmatter is kept as-is — byte parity
  // is defined against the fixture which retains it.
  let body = raw;
  if (config.frontmatter === null && body.startsWith('---\n')) {
    const end = body.indexOf('\n---', 4);
    if (end !== -1) {
      let cut = end + '\n---'.length;
      if (body[cut] === '\n') cut += 1;
      body = body.slice(cut);
    }
  }

  // Two-pass substitution.
  body = renderSpawnPlaceholders(body, config);
  body = applyBareTokens(body, config, isGlobal);

  return body;
}

/**
 * Render an agent file (Claude only). Substitutes `{{MODEL}}` with the model
 * hint from AGENT_MODELS. For `state-encoder` (no `model:` key), returns the
 * body unchanged.
 */
async function renderAgentFile(agentName: string, config: PlatformConfig): Promise<string> {
  const agentPath = join(ASSETS_DIR, 'templates', 'base', 'agents', `${agentName}.md`);
  let raw: string;
  try {
    raw = await readFile(agentPath, 'utf-8');
  } catch (err) {
    throw new Error(
      `renderAgentFile: cannot read agent body ${agentPath}: ${(err as Error).message}`,
    );
  }
  const model = AGENT_MODELS[agentName];
  let body = raw;
  if (model) {
    body = body.replace(/\{\{MODEL\}\}/g, renderModel(config, model));
  }
  // Agent bodies may reference HOOKS_PATH too.
  body = body
    .replace(/\{\{HOOKS_PATH\}\}/g, renderHooksPath(config, false))
    .replace(/\{\{ASK_USER_BLOCK\}\}/g, renderAskUserBlock(config))
    .replace(/\{\{SESSION_START_BOOTSTRAP\}\}/g, renderSessionStartBootstrap(config));
  body = renderSpawnPlaceholders(body, config);
  return body;
}

/** List skill names (basenames sans `.md`) from the base templates dir. */
async function listSkills(): Promise<string[]> {
  const baseDir = join(ASSETS_DIR, 'templates', 'base');
  let entries: string[];
  try {
    entries = await readdir(baseDir);
  } catch (err) {
    throw new Error(`listSkills: cannot read ${baseDir}: ${(err as Error).message}`);
  }
  return entries
    .filter((name) => name.endsWith('.md'))
    .map((name) => name.slice(0, -'.md'.length))
    .sort();
}

/** List agent names (basenames sans `.md`) from the base/agents dir. */
async function listAgents(): Promise<string[]> {
  const agentsDir = join(ASSETS_DIR, 'templates', 'base', 'agents');
  if (!(await exists(agentsDir))) return [];
  let entries: string[];
  try {
    entries = await readdir(agentsDir);
  } catch (err) {
    throw new Error(`listAgents: cannot read ${agentsDir}: ${(err as Error).message}`);
  }
  return entries
    .filter((name) => name.endsWith('.md'))
    .map((name) => name.slice(0, -'.md'.length))
    .sort();
}

/** Apply placeholder substitution to an extras file (same pipeline as skills). */
function applyPlaceholders(body: string, config: PlatformConfig, isGlobal: boolean): string {
  let out = renderSpawnPlaceholders(body, config);
  out = applyBareTokens(out, config, isGlobal);
  return out;
}

/**
 * C12 — Generate all platform files for a specific AI type.
 *
 * Responsibilities:
 *   1. Load the platform config.
 *   2. For each base skill, emit the skill file (with or without subdir per
 *      `skillNamePrefix`), copy its `{skill}.extra/` dir if present.
 *   3. For Claude (per_agent_model=true), emit standalone agent files.
 *   4. Walk `config.extraFiles` and emit each with placeholder substitution.
 */
export async function generatePlatformFiles(
  targetDir: string,
  aiType: string,
  isGlobal = false,
): Promise<string[]> {
  const config = await loadPlatformConfig(aiType);
  const createdPaths: string[] = [];

  const effectiveDir = isGlobal ? homedir() : targetDir;
  const rootDir = join(effectiveDir, config.folderStructure.root);
  const skillsParent = join(rootDir, config.folderStructure.skillPath);
  await mkdir(skillsParent, { recursive: true });

  const skills = await listSkills();
  const flatLayout = typeof config.skillNamePrefix === 'string' && config.skillNamePrefix.length > 0;

  for (const skillName of skills) {
    const rendered = await renderSkillFile(config, skillName, isGlobal);

    let skillDir: string;
    let skillFilePath: string;
    if (flatLayout) {
      // copilot-style flat layout: {root}/{skillPath}/{prefix}{skill}.prompt.md
      skillDir = skillsParent;
      const prefix = config.skillNamePrefix as string;
      skillFilePath = join(skillDir, `${prefix}${skillName}.prompt.md`);
    } else {
      // Per-skill subdir layout.
      skillDir = join(skillsParent, skillName);
      await mkdir(skillDir, { recursive: true });
      skillFilePath = join(skillDir, config.folderStructure.filename);
    }

    await writeFile(skillFilePath, rendered, 'utf-8');
    createdPaths.push(skillFilePath);

    // Copy {skill}.extra/ into the skill directory (only for non-flat layouts).
    if (!flatLayout) {
      const extraDir = join(ASSETS_DIR, 'templates', 'base', `${skillName}.extra`);
      if (await exists(extraDir)) {
        try {
          await cp(extraDir, skillDir, { recursive: true });
        } catch (err) {
          throw new Error(
            `generatePlatformFiles: failed to copy ${extraDir} -> ${skillDir}: ${(err as Error).message}`,
          );
        }
      }
    }
  }

  // Emit standalone agent files for Claude-class harnesses only.
  if (config.capabilities.per_agent_model) {
    const agentsDir = join(rootDir, 'agents');
    await mkdir(agentsDir, { recursive: true });
    for (const agentName of await listAgents()) {
      const rendered = await renderAgentFile(agentName, config);
      const agentPath = join(agentsDir, `${agentName}.md`);
      await writeFile(agentPath, rendered, 'utf-8');
      createdPaths.push(agentPath);
    }
    // Copy _shared/ if present, verbatim.
    const sharedSrc = join(ASSETS_DIR, 'templates', 'base', 'agents', '_shared');
    if (await exists(sharedSrc)) {
      const sharedDst = join(agentsDir, '_shared');
      try {
        await cp(sharedSrc, sharedDst, { recursive: true });
      } catch (err) {
        throw new Error(
          `generatePlatformFiles: failed to copy shared agent fragments: ${(err as Error).message}`,
        );
      }
    }
  }

  // Walk extraFiles.
  if (Array.isArray(config.extraFiles)) {
    for (const entry of config.extraFiles) {
      if (!entry || typeof entry.source !== 'string' || typeof entry.target !== 'string') {
        throw new Error(
          `generatePlatformFiles: invalid extraFiles entry: ${JSON.stringify(entry)}`,
        );
      }
      if (entry.source.includes('..') || entry.target.includes('..')) {
        throw new Error(
          `generatePlatformFiles: extraFiles paths must not contain "..": ${JSON.stringify(entry)}`,
        );
      }
      const sourcePath = join(ASSETS_DIR, 'templates', entry.source);
      let raw: string;
      try {
        raw = await readFile(sourcePath, 'utf-8');
      } catch (err) {
        throw new Error(
          `generatePlatformFiles: cannot read extras source ${sourcePath}: ${(err as Error).message}`,
        );
      }
      const rendered = applyPlaceholders(raw, config, isGlobal);
      // extraFiles paths are relative to the effective install root (e.g.
      // `hooks.json` sits next to `.claude/`, not inside it).
      const targetPath = join(effectiveDir, entry.target);
      await mkdir(dirname(targetPath), { recursive: true });
      await writeFile(targetPath, rendered, 'utf-8');
      createdPaths.push(targetPath);
    }
  }

  return createdPaths;
}

/** C12 — Generate files for every harness (skips `all` sentinel). */
export async function generateAllPlatformFiles(
  targetDir: string,
  isGlobal = false,
): Promise<string[]> {
  const allPaths: string[] = [];
  for (const aiType of Object.keys(AI_TO_PLATFORM)) {
    try {
      const paths = await generatePlatformFiles(targetDir, aiType, isGlobal);
      allPaths.push(...paths);
    } catch (err) {
      // Surface per-harness failures but continue so a single bad platform
      // does not abort the `--ai all` flow.
      // eslint-disable-next-line no-console
      console.error(`generateAllPlatformFiles: ${aiType} failed: ${(err as Error).message}`);
    }
  }
  return allPaths;
}

