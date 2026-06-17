# CLAUDE.md

Guidance for Claude Code and other Claude-based agents working in this repository.

## Release Hygiene

Every PR or code update that changes dynos-work must include a tiny version bump and a changelog entry.

- Current release line: `7.5.1`.
- Use patch-style nightly increments for ordinary updates: `7.5.1` -> `7.5.2` -> `7.5.3`, and so on.
- Update every plugin/package version field together:
  - `package.json`
  - `.claude-plugin/plugin.json`
  - `.claude-plugin/marketplace.json`
  - `.codex-plugin/plugin.json`
- Add a matching entry to `CHANGELOG.md` for the new version before opening the PR.
- Keep the changelog entry small but concrete: summarize the behavior change, fix, or maintenance work that justifies the version bump.

Do not open or prepare a PR with code changes while leaving the version and changelog unchanged.
