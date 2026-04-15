# Migration guide: dynos-work 6.0.0 -> 7.0.0

This release restructures how dynos-work is distributed. The workflow, skill
names, and agent contracts are unchanged. What changed is *where* the artefacts
live and *how* they reach your harness.

In 6.0.0 the Claude plugin tree was checked into the repo at
`skills/`, `agents/`, `hooks.json`, `.claude-plugin/plugin.json`, and Claude
Code's marketplace installed it directly from the repo.

In 7.0.0 those artefacts are **emitted locally** by `dynos-work-cli`. The repo
ships base templates and per-harness capability flags; the CLI renders the
harness-specific tree on your machine. Eighteen AI coding harnesses are now
supported by the same source-of-truth.

---

## 1. Upgrading from 6.0.0 (Claude Code users)

If you previously installed dynos-work into Claude Code, do this once:

```bash
npx dynos-work-cli init --ai claude --target .
```

Or, equivalently, from a dynos-work repo checkout:

```bash
./install.sh
```

Either command regenerates `.claude/skills/dynos-work/`, `.claude/agents/`,
`hooks.json`, and `.claude-plugin/plugin.json` in your project. The output is
semantically identical to the 6.0.0 Claude tree, with two visible diffs:

1. The `${PLUGIN_HOOKS}` token used in 6.0.0 skill bodies is rewritten to
   `${CLAUDE_PLUGIN_ROOT}/hooks` on emit — this is the canonical Claude form.
2. `.claude-plugin/plugin.json` now declares `"version": "7.0.0"`.

No manual edits are required. The parity gate
(`scripts/check-claude-parity.sh`) enforces byte-identity for `agents/` and
`hooks.json` and semantic identity for skill bodies.

If you edited the checked-in 6.0.0 tree locally, your edits will be lost on
regen. Move them into `cli/assets/templates/base/` (or a downstream overlay)
and open a PR.

---

## 2. First-time install for non-Claude harnesses

Pick your harness and run the matching command from the project root you want
dynos-work installed into:

| Harness              | Install command                                     |
|----------------------|-----------------------------------------------------|
| Claude Code          | `npx dynos-work-cli init --ai claude`               |
| Cursor               | `npx dynos-work-cli init --ai cursor`               |
| Windsurf             | `npx dynos-work-cli init --ai windsurf`             |
| Google Antigravity   | `npx dynos-work-cli init --ai antigravity`          |
| GitHub Copilot       | `npx dynos-work-cli init --ai copilot`              |
| Kiro                 | `npx dynos-work-cli init --ai kiro`                 |
| OpenAI Codex         | `npx dynos-work-cli init --ai codex`                |
| Roo Code             | `npx dynos-work-cli init --ai roocode`              |
| Qoder                | `npx dynos-work-cli init --ai qoder`                |
| Gemini CLI           | `npx dynos-work-cli init --ai gemini`               |
| Trae                 | `npx dynos-work-cli init --ai trae`                 |
| OpenCode             | `npx dynos-work-cli init --ai opencode`             |
| Continue             | `npx dynos-work-cli init --ai continue`             |
| CodeBuddy            | `npx dynos-work-cli init --ai codebuddy`            |
| Factory Droid        | `npx dynos-work-cli init --ai droid`                |
| Kilo Code            | `npx dynos-work-cli init --ai kilocode`             |
| Warp                 | `npx dynos-work-cli init --ai warp`                 |
| Augment Code         | `npx dynos-work-cli init --ai augment`              |

Each command writes the harness-specific folder layout (e.g. `.claude/`,
`.cursor/`, `.agents/`, `.github/copilot/`, etc.). Existing files in those
target paths are overwritten; back up any local customisations first.

After install, also run `./install.sh` once (or manually add
`~/.dynos-work/bin` to your PATH) to get the `dynos` Python CLI on your PATH
— this powers the local runtime daemon, registry, and autofix.

---

## 3. Context-budget warnings

Some harnesses have substantially smaller context windows than Claude. The
execution skill (`execute.md`) inlines the bodies of 7+ specialist agents plus
the task spec and plan, and can easily exceed a 32k token budget.

Affected harnesses (non-exhaustive):

- **Warp** — shell-first harness, small context.
- **GitHub Copilot** (older chat modes) — tight context.
- **Factory Droid** (standard tier) — moderate budget.

Mitigations:

1. **Split execution into several sessions.** Run `execute` once per
   execution-graph segment rather than the whole graph at once. The
   `.dynos/task-*/state.json` is persistent and resumable.
2. **Prefer "minimal" mode.** Some platform manifests support a `minimal`
   install variant that omits inlined agent bodies and expects the harness to
   fan out via tool-call. Use `--minimal` on `init` where available.
3. **Ignore audit on small harnesses.** The audit skill is the single largest
   skill body; if context is tight, skip it and run `scripts/check-claude-parity.sh`
   in CI instead.

If your harness hits a context-blowup, file an issue with the harness name
and the failing skill — we will tag it with a smaller-context flag in the
platform manifest.

---

## 4. Development workflow

If you are editing dynos-work itself:

```bash
# 1. Edit base templates (single source of truth)
vim cli/assets/templates/base/skills/start.md

# 2. Build the CLI
cd cli
bun install
bun run build

# 3. Regenerate locally for your harness of choice
cd ..
./install.sh --develop     # uses cli/dist/index.js, copies hooks/ + bin/ from repo

# 4. Verify parity with the frozen Claude fixture
./scripts/check-claude-parity.sh
./scripts/check-renderer-drift.sh
```

`--develop` mode:

- Uses the locally-built `cli/dist/index.js` instead of fetching via `npx`.
- Copies runtime `hooks/` and `bin/` from the repo checkout into
  `~/.dynos-work/{hooks,bin}/` (the `--production` path expects those under
  `cli/assets/hooks/` + `cli/assets/bin/` as shipped by npm).
- Useful when iterating on hooks, templates, or the renderer.

Edits to `cli/src/**`, `cli/assets/templates/**`, and the platform manifests
take effect on the next `bun run build` + `./install.sh --develop`.

---

## Questions, bug reports, regressions

File an issue at https://github.com/dynos-fit/dynos-work with the label
`migration-7.0` and include:

- Your harness and version
- The output of `npx dynos-work-cli init --ai <platform> --target /tmp/dw-test --verbose`
- Any diff between the old 6.0.0 tree and the regenerated 7.0.0 tree that
  broke your workflow.
