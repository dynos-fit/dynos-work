#!/usr/bin/env bun
/**
 * Thin wrapper around `Bun.build` for programmatic builds.
 * Equivalent to: `bun build src/index.ts --outdir dist --target node`.
 *
 * After bundling, copies the repo-root `hooks/` and `bin/` trees into
 * `assets/hooks/` and `assets/bin/` so the published npm tarball ships a
 * self-contained runtime payload (sca-004). `__pycache__` dirs are skipped.
 */

import { cp, mkdir, rm, stat } from 'node:fs/promises';
import { join, resolve } from 'node:path';

async function pathExists(p: string): Promise<boolean> {
  try {
    await stat(p);
    return true;
  } catch {
    return false;
  }
}

async function copyRuntimeAssets(): Promise<void> {
  const cliDir = resolve(import.meta.dir);
  const repoRoot = resolve(cliDir, '..');

  const pairs: Array<{ src: string; dst: string; label: string }> = [
    { src: join(repoRoot, 'hooks'), dst: join(cliDir, 'assets', 'hooks'), label: 'hooks' },
    { src: join(repoRoot, 'bin'),   dst: join(cliDir, 'assets', 'bin'),   label: 'bin'   },
  ];

  for (const { src, dst, label } of pairs) {
    if (!(await pathExists(src))) {
      console.warn(`[build] skip ${label}: source missing at ${src}`);
      continue;
    }
    // Clean first so stale files from an older build don't linger in the tarball.
    if (await pathExists(dst)) {
      await rm(dst, { recursive: true, force: true });
    }
    await mkdir(dst, { recursive: true });
    await cp(src, dst, {
      recursive: true,
      filter: (source: string) => {
        // Skip Python bytecode caches — they bloat the tarball and are host-specific.
        if (source.includes(`${'/'}__pycache__`) || source.endsWith('/__pycache__')) {
          return false;
        }
        if (source.endsWith('.pyc')) {
          return false;
        }
        return true;
      },
    });
    console.log(`[build] copied ${label}/ -> assets/${label}/`);
  }
}

async function main(): Promise<void> {
  // The bundle step is invoked by package.json's build script
  // (`bun build src/index.ts --outdir dist --target node`) — this file
  // only performs the asset copy that must happen after bundling.
  await copyRuntimeAssets();
}

main().catch((err) => {
  console.error('Build crashed:', err instanceof Error ? err.message : err);
  process.exit(1);
});
