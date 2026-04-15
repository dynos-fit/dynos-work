import type { AIType } from '../types/index.js';
import { logger } from '../utils/logger.js';

export interface UninstallOptions {
  ai?: AIType;
  global?: boolean;
}

/**
 * Stub: uninstall is not yet implemented. Exits non-zero to avoid
 * silently misleading callers (and automation) into thinking it succeeded.
 */
export async function uninstallCommand(
  options: UninstallOptions,
): Promise<void> {
  logger.title('dynos-work-cli uninstall');
  if (options.ai) {
    logger.info(`Requested harness: ${options.ai}`);
  }
  if (options.global) {
    logger.info('Mode: global');
  }
  logger.error(
    'uninstall not yet implemented in v7.0.0 — see https://github.com/dynos-fit/dynos-work/issues for roadmap',
  );
  process.exitCode = 1;
}
