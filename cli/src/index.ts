#!/usr/bin/env node

import { Command } from 'commander';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

import { initCommand } from './commands/init.js';
import { versionsCommand } from './commands/versions.js';
import { updateCommand } from './commands/update.js';
import { uninstallCommand } from './commands/uninstall.js';
import { AI_TYPES, type AIType } from './types/index.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

function readVersion(): string {
  try {
    const pkgRaw = readFileSync(join(__dirname, '..', 'package.json'), 'utf-8');
    const pkg = JSON.parse(pkgRaw) as { version?: string };
    return pkg.version ?? '0.0.0';
  } catch {
    return '0.0.0';
  }
}

function assertValidAI(ai: string | undefined): void {
  if (ai && !(AI_TYPES as readonly string[]).includes(ai)) {
    console.error(`Invalid AI type: ${ai}`);
    console.error(`Valid types: ${AI_TYPES.join(', ')}`);
    process.exit(1);
  }
}

const program = new Command();

program
  .name('dynos-work-cli')
  .description('CLI to install dynos-work across AI coding assistants')
  .version(readVersion());

program
  .command('init')
  .description('Install dynos-work to current project')
  .option('-a, --ai <type>', `AI assistant type (${AI_TYPES.join(', ')})`)
  .option('-t, --target <dir>', 'Target directory for installation')
  .option('-g, --global', 'Install globally to home directory (~/)')
  .option('-p, --project', 'Install project-locally (override a harness default-scope of global)')
  .option('-f, --force', 'Overwrite existing files')
  .option('-o, --offline', 'Skip network, use bundled assets only')
  .action(async (options) => {
    assertValidAI(options.ai);
    await initCommand({
      ai: options.ai as AIType | undefined,
      target: options.target,
      global: options.global,
      project: options.project,
      force: options.force,
      offline: options.offline,
    });
  });

program
  .command('versions')
  .description('List available versions')
  .action(async () => {
    await versionsCommand();
  });

program
  .command('update')
  .description('Update dynos-work to latest version')
  .option('-a, --ai <type>', `AI assistant type (${AI_TYPES.join(', ')})`)
  .action(async (options) => {
    assertValidAI(options.ai);
    await updateCommand({
      ai: options.ai as AIType | undefined,
    });
  });

program
  .command('uninstall')
  .description('Remove dynos-work from current project or globally')
  .option('-a, --ai <type>', `AI assistant type (${AI_TYPES.join(', ')})`)
  .option('-g, --global', 'Uninstall from home directory (~/)')
  .action(async (options) => {
    assertValidAI(options.ai);
    await uninstallCommand({
      ai: options.ai as AIType | undefined,
      global: options.global,
    });
  });

program.parseAsync().catch((err) => {
  console.error(err instanceof Error ? err.message : String(err));
  process.exit(1);
});
