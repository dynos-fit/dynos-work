import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { logger } from '../utils/logger.js';

const __dirname = dirname(fileURLToPath(import.meta.url));

/**
 * Print the installed dynos-work-cli version. Reads from the package.json
 * bundled alongside the built entry point. Silent-safe: prints "unknown" and
 * exits 0 if the package.json cannot be located (e.g. during dev-time runs
 * from an unusual cwd).
 */
export async function versionsCommand(): Promise<void> {
  logger.title('dynos-work-cli versions');

  // dist layout: dist/index.js -> ../package.json
  // dev layout:  src/commands/versions.ts -> ../../package.json
  const candidates = [
    join(__dirname, '..', 'package.json'),
    join(__dirname, '..', '..', 'package.json'),
  ];
  for (const p of candidates) {
    try {
      const raw = readFileSync(p, 'utf-8');
      const pkg = JSON.parse(raw) as { version?: string; name?: string };
      if (pkg.version) {
        logger.info(`${pkg.name ?? 'dynos-work-cli'} ${pkg.version}`);
        return;
      }
    } catch {
      // try next candidate
    }
  }
  logger.warn('version unknown (package.json not found on disk)');
}
