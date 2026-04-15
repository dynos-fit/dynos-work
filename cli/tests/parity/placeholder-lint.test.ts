/**
 * Tests that no `{{ }}` placeholder residue leaks into rendered outputs.
 *
 * Covers: Risk R5 (placeholder typos silently degrade output) mitigation.
 * Applies to every supported harness — each regenerated output must have zero
 * /\{\{[^}]*\}\}/ matches across all rendered files.
 */
import { describe, it, expect } from 'bun:test';
import { existsSync, mkdtempSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';

const CLI_DIR = resolve(import.meta.dir, '..', '..');
const DIST_ENTRY = join(CLI_DIR, 'dist', 'index.js');

// Small, representative sweep. Claude included so we also catch Claude-path regressions.
const HARNESSES = ['claude', 'cursor', 'windsurf', 'copilot', 'warp'];

function ensureBuilt() {
  if (!existsSync(DIST_ENTRY)) {
    throw new Error(`dist/index.js missing at ${DIST_ENTRY}. Build the CLI before running parity tests.`);
  }
}

function regenerate(harness: string): string {
  ensureBuilt();
  const target = mkdtempSync(join(tmpdir(), `dw-lint-${harness}-`));
  const result = spawnSync('node', [DIST_ENTRY, 'init', '--ai', harness, '--target', target], {
    encoding: 'utf8',
  });
  if (result.status !== 0) {
    throw new Error(`regen for ${harness} failed (exit ${result.status}):\n${result.stderr}`);
  }
  return target;
}

function walk(dir: string, rel = ''): string[] {
  if (!existsSync(dir)) return [];
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    const relPath = rel ? join(rel, entry) : entry;
    const st = statSync(full);
    if (st.isDirectory()) out.push(...walk(full, relPath));
    else out.push(relPath);
  }
  return out;
}

describe('placeholder residue lint', () => {
  it.each(HARNESSES.map((h) => [h]))(
    '%s output contains no stray `{{…}}` placeholders',
    (harness) => {
      const t = regenerate(harness);
      const files = walk(t).filter((f) => f.endsWith('.md') || f.endsWith('.json'));
      expect(files.length).toBeGreaterThan(0);
      // Only template-style placeholders count: `{{UPPERCASE_NAME}}` or `{{PREFIX:payload}}`.
      // We specifically exclude matches that appear inside inline-code spans (single or
      // triple backticks), because a legitimate skill body may contain prose like
      // "no `{{` or `}}` sequences" as an instructional string.
      const placeholderRe = /\{\{[A-Z][A-Z0-9_]*(?::[^}]*)?\}\}/;
      for (const rel of files) {
        const body = readFileSync(join(t, rel), 'utf8');
        const m = body.match(placeholderRe);
        if (m) {
          throw new Error(`${harness}:${rel} contains residual placeholder: ${m[0]}`);
        }
      }
    },
  );
});
