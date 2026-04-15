/**
 * Integration smoke test: `init --ai <harness>` across all 18 harnesses.
 *
 * Covers: general robustness of generatePlatformFiles across every platform JSON.
 * Each harness is regenerated into a fresh tmpdir and the test asserts:
 *   1. CLI exits 0
 *   2. At least one non-empty rendered file was written somewhere in the output dir
 *
 * NOTE: this is a smoke test, not a correctness check. Harness-specific shape
 * assertions live in parity/claude.test.ts and parity/non-claude.test.ts.
 */
import { describe, it, expect } from 'bun:test';
import { existsSync, mkdtempSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const CLI_DIR = resolve(import.meta.dir, '..', '..');
const DIST_ENTRY = join(CLI_DIR, 'dist', 'index.js');

const HARNESSES = [
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
];

function walk(dir: string): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full));
    else out.push(full);
  }
  return out;
}

describe('init --ai <harness> smoke', () => {
  it.each(HARNESSES.map((h) => [h]))(
    '%s: CLI exits 0 and writes at least one non-empty file',
    (harness) => {
      if (!existsSync(DIST_ENTRY)) {
        throw new Error(`CLI not built: ${DIST_ENTRY}`);
      }
      const target = mkdtempSync(join(tmpdir(), `dw-smoke-${harness}-`));
      // Force --project so the target dir gets the files even for harnesses
      // that default to global scope (codex). This test asserts --target
      // routing, not scope defaults.
      const result = spawnSync(
        'node',
        [DIST_ENTRY, 'init', '--ai', harness, '--target', target, '--project'],
        { encoding: 'utf8' },
      );
      expect(result.status).toBe(0);

      const files = walk(target);
      expect(files.length).toBeGreaterThan(0);

      // At least one file should be non-empty to prove the renderer actually wrote content
      const hasContent = files.some((f) => readFileSync(f, 'utf8').length > 0);
      expect(hasContent).toBe(true);
    },
    60_000,
  );
});
