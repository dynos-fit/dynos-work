# CLAUDE.md

Guidance for Claude Code and other Claude-based agents working in this repository.

## Release Hygiene

Every PR that changes dynos-work ships as exactly one release. **The version number is computed by CI — do not edit it by hand.**

### What CI does

`.github/workflows/release-hygiene.yml` runs on every PR open, synchronize, reopen, and label change. It calls `scripts/bump_version.py`, which:

- reads the current version from the PR's **base ref** (not from your branch),
- derives the bump from the PR title's conventional-commit prefix: `feat:` is a minor bump, `fix:` / `perf:` / `refactor:` / `docs:` / `chore:` / `test:` / `ci:` / `build:` / `style:` / `revert:` are patch bumps, and a `!` after the type (`feat!:`, `fix(api)!:`) or the words `BREAKING CHANGE` make it major. A `release:major` / `release:minor` / `release:patch` / `release:none` label overrides the title. A title with no recognized prefix fails the job until it is retitled (retitling re-runs it),
- writes the result to all four manifests (`package.json`, `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `.codex-plugin/plugin.json`),
- adds a stub `CHANGELOG.md` section only if no entry for that version exists,
- and pushes a `chore: update release metadata` commit to your branch.

It is idempotent: re-running produces the same version, so a PR gets one version no matter how many commits it contains.

### What you do

- **Prefix the PR title.** `feat: …` for new behavior, `fix: …` for bug fixes, `docs:` / `chore:` / `refactor:` / `test:` / `ci:` for everything else, `feat!:` for breaking changes. The prefix decides the version, so pick it for what the change means to a plugin user, not for its size.
- **Write the `CHANGELOG.md` entry.** CI's stub is a fallback, not a substitute — it only records the PR title. Describe the behavior change, fix, or maintenance work concretely.
- **Leave the four manifest version fields alone.** Hand-editing them is overwritten by CI, and the bot's push to your branch will reject your next `git push` until you rebase.
- **One entry per PR, not per change.** If a PR bundles several logical changes, they still ship under one version. Writing an entry per change produces orphan entries for versions no manifest ever carried.

If you need to know the version your PR will land as, apply your title's bump to the base branch's version: `feat:` raises the minor and zeroes the patch, anything else raises the patch (or whatever your `release:*` label selects). Write the CHANGELOG entry under that version header, not under `[Unreleased]`: CI only adds a stub when the header is missing and never promotes Unreleased notes. `scripts/bump_version.py` has no dry-run mode — it writes the manifests in place — so read the number off `git show origin/main:package.json` rather than running the script to find out.

Do not open or prepare a PR with code changes while leaving `CHANGELOG.md` unchanged.
