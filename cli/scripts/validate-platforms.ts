#!/usr/bin/env bun
/**
 * validate-platforms.ts
 *
 * Reads every JSON file in cli/assets/templates/platforms/ and validates it
 * against cli/assets/templates/platforms/platform.schema.json using ajv.
 *
 * Exits 0 on full pass; 1 on any parse or validation error.
 */

import { readdirSync, readFileSync, existsSync, statSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import Ajv, { type ErrorObject, type ValidateFunction } from 'ajv';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const PLATFORMS_DIR = resolve(__dirname, '..', 'assets', 'templates', 'platforms');
const SCHEMA_FILE = 'platform.schema.json';

// 18 required platform files (D9: antigravity lives in agent.json).
const REQUIRED_PLATFORM_FILES = [
  'claude.json',
  'cursor.json',
  'windsurf.json',
  'agent.json',
  'copilot.json',
  'kiro.json',
  'codex.json',
  'roocode.json',
  'qoder.json',
  'gemini.json',
  'trae.json',
  'opencode.json',
  'continue.json',
  'codebuddy.json',
  'droid.json',
  'kilocode.json',
  'warp.json',
  'augment.json',
] as const;

interface ValidationFailure {
  file: string;
  reason: string;
  errors?: ErrorObject[] | null;
}

function readJsonSafe(file: string): { ok: true; data: unknown } | { ok: false; error: string } {
  try {
    const text = readFileSync(file, 'utf8');
    return { ok: true, data: JSON.parse(text) };
  } catch (err) {
    return { ok: false, error: (err as Error).message };
  }
}

function compileSchema(): ValidateFunction {
  const schemaPath = join(PLATFORMS_DIR, SCHEMA_FILE);
  if (!existsSync(schemaPath)) {
    throw new Error(`Schema file not found: ${schemaPath}`);
  }
  const parsed = readJsonSafe(schemaPath);
  if (!parsed.ok) {
    throw new Error(`Schema is not valid JSON (${schemaPath}): ${parsed.error}`);
  }
  const ajv = new Ajv({ allErrors: true, strict: false });
  try {
    return ajv.compile(parsed.data as object);
  } catch (err) {
    throw new Error(`Failed to compile schema: ${(err as Error).message}`);
  }
}

function main(): number {
  if (!existsSync(PLATFORMS_DIR) || !statSync(PLATFORMS_DIR).isDirectory()) {
    console.error(`[validate-platforms] platforms dir not found: ${PLATFORMS_DIR}`);
    return 1;
  }

  let validate: ValidateFunction;
  try {
    validate = compileSchema();
  } catch (err) {
    console.error(`[validate-platforms] ${(err as Error).message}`);
    return 1;
  }

  const entries = readdirSync(PLATFORMS_DIR).filter((f) => f.endsWith('.json') && f !== SCHEMA_FILE);

  // Presence check: 18 required files.
  const failures: ValidationFailure[] = [];
  for (const required of REQUIRED_PLATFORM_FILES) {
    if (!entries.includes(required)) {
      failures.push({ file: required, reason: 'missing required platform file' });
    }
  }

  // Also surface any *extra* JSONs (not fatal — reported for visibility).
  const extras = entries.filter((f) => !REQUIRED_PLATFORM_FILES.includes(f as (typeof REQUIRED_PLATFORM_FILES)[number]));

  let passCount = 0;
  for (const f of entries) {
    const fullPath = join(PLATFORMS_DIR, f);
    const parsed = readJsonSafe(fullPath);
    if (!parsed.ok) {
      failures.push({ file: f, reason: `invalid JSON: ${parsed.error}` });
      continue;
    }
    const ok = validate(parsed.data);
    if (!ok) {
      failures.push({ file: f, reason: 'schema validation failed', errors: validate.errors ?? null });
      continue;
    }
    passCount++;
  }

  // Report.
  console.log(`[validate-platforms] schema: ${SCHEMA_FILE}`);
  console.log(`[validate-platforms] platforms dir: ${PLATFORMS_DIR}`);
  console.log(`[validate-platforms] JSON files discovered: ${entries.length}`);
  console.log(`[validate-platforms] required platforms expected: ${REQUIRED_PLATFORM_FILES.length}`);
  console.log(`[validate-platforms] validated OK: ${passCount}`);

  if (extras.length > 0) {
    console.log(`[validate-platforms] extra (non-required) JSONs present: ${extras.join(', ')}`);
  }

  if (failures.length === 0) {
    console.log('[validate-platforms] all platform JSONs pass schema validation.');
    return 0;
  }

  console.error(`[validate-platforms] FAILED with ${failures.length} error(s):`);
  for (const fail of failures) {
    console.error(`  - ${fail.file}: ${fail.reason}`);
    if (fail.errors) {
      for (const e of fail.errors) {
        console.error(`      @${e.instancePath || '/'} ${e.keyword}: ${e.message ?? ''} ${JSON.stringify(e.params)}`);
      }
    }
  }
  return 1;
}

try {
  const code = main();
  process.exit(code);
} catch (err) {
  console.error(`[validate-platforms] unexpected error: ${(err as Error).message}`);
  process.exit(1);
}
