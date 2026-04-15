import { resolve } from 'node:path';

import type { AIType } from '../types/index.ts';
import { hasCapability } from '../utils/capability.ts';
import { detectAIType, getAITypeDescription } from '../utils/detect.ts';
import { logger } from '../utils/logger.ts';
import {
  AI_TO_PLATFORM,
  generateAllPlatformFiles,
  generatePlatformFiles,
  loadPlatformConfig,
} from '../utils/template.ts';

export interface InitOptions {
  ai?: AIType;
  target?: string;
  global?: boolean;
  force?: boolean;
  offline?: boolean;
}

/**
 * Initialize a dynos-work install for one harness or all of them.
 *
 * When `--ai` is omitted we try to auto-detect a harness from sentinel dirs in
 * the target directory. Zero matches -> surface the supported list and exit 1.
 * Exactly one match -> use it. Multiple matches -> ambiguous; list candidates
 * and ask the user to pass `--ai` explicitly.
 */
export async function initCommand(options: InitOptions): Promise<void> {
  logger.title('dynos-work-cli init');

  const isGlobal = Boolean(options.global);
  const targetDir = resolve(options.target ?? process.cwd());

  let ai = options.ai;
  if (!ai) {
    const result = detectAIType(targetDir);
    if (result.detected.length === 0) {
      logger.error('No AI harness detected in target directory.');
      logger.info('Supported harnesses:');
      for (const candidate of Object.keys(AI_TO_PLATFORM)) {
        logger.dim(`  - ${candidate}: ${getAITypeDescription(candidate as AIType)}`);
      }
      logger.error('Pass --ai <harness> explicitly. See `dynos-work-cli --help`.');
      process.exitCode = 1;
      return;
    }
    if (result.detected.length === 1) {
      ai = result.detected[0] as AIType;
      logger.info(`Auto-detected harness: ${getAITypeDescription(ai)}`);
    } else {
      logger.error('Multiple AI harnesses detected; pass --ai <harness> explicitly:');
      for (const candidate of result.detected) {
        logger.dim(`  - ${candidate}: ${getAITypeDescription(candidate)}`);
      }
      process.exitCode = 1;
      return;
    }
  }

  logger.info(`Harness: ${ai} (${getAITypeDescription(ai)})`);
  logger.info(`Target: ${isGlobal ? '$HOME (global install)' : targetDir}`);

  // Informational: report whether hooks.json will be emitted for this harness.
  // Rationale: `hasCapability` encodes the fail-closed check against the
  // capability map, so a harness that forgot to declare `lifecycle_hooks` will
  // be flagged instead of silently skipping hook emission.
  if (ai !== 'all') {
    try {
      const config = await loadPlatformConfig(ai);
      if (hasCapability(config, 'lifecycle_hooks')) {
        logger.info('Harness supports lifecycle hooks — will write hooks.json');
      } else {
        logger.info('Harness has no lifecycle-hook support — skipping hooks.json');
      }
    } catch (err) {
      logger.warn(`capability probe failed: ${(err as Error).message}`);
    }
  }

  try {
    let writtenPaths: string[];
    if (ai === 'all') {
      writtenPaths = await generateAllPlatformFiles(targetDir, isGlobal);
    } else {
      writtenPaths = await generatePlatformFiles(targetDir, ai, isGlobal);
    }
    logger.success(`Wrote ${writtenPaths.length} file(s).`);
  } catch (err) {
    logger.error(`init failed: ${(err as Error).message}`);
    process.exitCode = 1;
  }
}
