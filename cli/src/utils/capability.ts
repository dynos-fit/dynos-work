import type { PlatformConfig } from '../types/index.js';

export type CapabilityKey =
  | 'parallel_subagents'
  | 'lifecycle_hooks'
  | 'transcript_parsing'
  | 'per_agent_model'
  | 'structured_questions'
  | 'env_injection';

/**
 * Returns true if the platform config declares the given capability flag as true.
 * Missing capability blocks or missing keys are treated as false (fail-closed).
 */
export function hasCapability(
  config: PlatformConfig,
  key: CapabilityKey,
): boolean {
  const caps = config?.capabilities;
  if (!caps) return false;
  const value = caps[key];
  return value === true;
}
