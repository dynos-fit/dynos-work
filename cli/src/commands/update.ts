import type { AIType } from '../types/index.js';
import { logger } from '../utils/logger.js';

export interface UpdateOptions {
  ai?: AIType;
}

/**
 * Stub: update is not yet implemented. Exits non-zero to avoid
 * silently misleading callers (and automation) into thinking it succeeded.
 */
export async function updateCommand(options: UpdateOptions): Promise<void> {
  logger.title('dynos-work-cli update');
  if (options.ai) {
    logger.info(`Requested harness: ${options.ai}`);
  }
  logger.error(
    'update not yet implemented in v7.0.0 — see https://github.com/dynos-fit/dynos-work/issues for roadmap',
  );
  process.exitCode = 1;
}
