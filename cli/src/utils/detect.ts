import { existsSync } from 'node:fs';
import { join } from 'node:path';
import type { AIType } from '../types/index.js';

export interface DetectionResult {
  detected: AIType[];
  suggested: AIType | null;
}

/**
 * Map of harness identifier -> sentinel directory (or directories) checked relative to cwd.
 * The first sentinel that exists triggers detection. `antigravity` accepts either `.agents`
 * or `.agent` for backwards compatibility.
 */
const SENTINELS: Record<Exclude<AIType, 'all'>, string[]> = {
  claude: ['.claude'],
  cursor: ['.cursor'],
  windsurf: ['.windsurf'],
  antigravity: ['.agents', '.agent'],
  copilot: ['.github'],
  kiro: ['.kiro'],
  codex: ['.codex'],
  roocode: ['.roo'],
  qoder: ['.qoder'],
  gemini: ['.gemini'],
  trae: ['.trae'],
  opencode: ['.opencode'],
  continue: ['.continue'],
  codebuddy: ['.codebuddy'],
  droid: ['.factory'],
  kilocode: ['.kilocode'],
  warp: ['.warp'],
  augment: ['.augment'],
};

export function detectAIType(cwd: string = process.cwd()): DetectionResult {
  const detected: AIType[] = [];

  for (const [ai, dirs] of Object.entries(SENTINELS) as [
    Exclude<AIType, 'all'>,
    string[],
  ][]) {
    try {
      if (dirs.some((d) => existsSync(join(cwd, d)))) {
        detected.push(ai);
      }
    } catch {
      // existsSync should not throw for normal paths, but be defensive.
      continue;
    }
  }

  let suggested: AIType | null = null;
  if (detected.length === 1) {
    suggested = detected[0] ?? null;
  } else if (detected.length > 1) {
    suggested = 'all';
  }

  return { detected, suggested };
}

export function getAITypeDescription(aiType: AIType): string {
  switch (aiType) {
    case 'claude':
      return 'Claude Code (.claude/skills/)';
    case 'cursor':
      return 'Cursor (.cursor/skills/)';
    case 'windsurf':
      return 'Windsurf (.windsurf/skills/)';
    case 'antigravity':
      return 'Antigravity (.agents/skills/)';
    case 'copilot':
      return 'GitHub Copilot (.github/prompts/)';
    case 'kiro':
      return 'Kiro (.kiro/steering/)';
    case 'codex':
      return 'Codex (.codex/skills/)';
    case 'roocode':
      return 'RooCode (.roo/skills/)';
    case 'qoder':
      return 'Qoder (.qoder/skills/)';
    case 'gemini':
      return 'Gemini CLI (.gemini/skills/)';
    case 'trae':
      return 'Trae (.trae/skills/)';
    case 'opencode':
      return 'OpenCode (.opencode/skills/)';
    case 'continue':
      return 'Continue (.continue/skills/)';
    case 'codebuddy':
      return 'CodeBuddy (.codebuddy/skills/)';
    case 'droid':
      return 'Droid (Factory) (.factory/skills/)';
    case 'kilocode':
      return 'KiloCode (.kilocode/skills/)';
    case 'warp':
      return 'Warp (.warp/skills/)';
    case 'augment':
      return 'Augment (.augment/skills/)';
    case 'all':
      return 'All AI assistants';
  }
}
