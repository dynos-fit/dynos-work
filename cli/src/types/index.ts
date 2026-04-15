/**
 * Type definitions for dynos-work-cli.
 *
 * AI_TYPES is the authoritative list of supported harness identifiers.
 * It contains exactly 18 harnesses + the special `all` selector (19 entries).
 */

export const AI_TYPES = [
  'claude',
  'cursor',
  'windsurf',
  'antigravity',
  'copilot',
  'kiro',
  'codex',
  'roocode',
  'qoder',
  'gemini',
  'trae',
  'opencode',
  'continue',
  'codebuddy',
  'droid',
  'kilocode',
  'warp',
  'augment',
  'all',
] as const;

export type AIType = (typeof AI_TYPES)[number];

export type InstallType = 'full' | 'reference';

export type SkillOrWorkflow = 'Skill' | 'Workflow';

export interface FolderStructure {
  root: string;
  skillPath: string;
  filename: string;
}

export interface PlatformCapabilities {
  parallel_subagents: boolean;
  lifecycle_hooks: boolean;
  transcript_parsing: boolean;
  per_agent_model: boolean;
  structured_questions: boolean;
  env_injection: boolean;
}

export interface ExtraFile {
  source: string;
  target: string;
}

export type DefaultScope = 'global' | 'project';

export interface PlatformConfig {
  platform: string;
  displayName: string;
  installType: InstallType;
  folderStructure: FolderStructure;
  frontmatter: Record<string, string> | null;
  capabilities: PlatformCapabilities;
  extraFiles?: ExtraFile[];
  skillNamePrefix?: string;
  scriptPath?: string;
  skillOrWorkflow?: SkillOrWorkflow;
  /**
   * Install scope default when the user doesn't pass --global or --project.
   * Codex reads skills from ~/.codex/skills/ (global only), so its default is
   * "global". Most other harnesses scan project-local dirs (.cursor/, .claude/,
   * etc.), so they default to "project" (absent field → "project").
   */
  defaultScope?: DefaultScope;
}
