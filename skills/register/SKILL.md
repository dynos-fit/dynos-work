---
name: register
description: "Internal dynos-work skill. Register the current project directory with the dynos-work global registry. Works from any project."
---

# dynos-work: Register

Registers the current working directory as a dynos-work project in the global registry at `~/.dynos/`.

## Ruthlessness Standard

- Do not claim registration succeeded unless the command succeeded.
- If the directory is already registered, say so explicitly.
- If hook resolution or registry write fails, report the exact failure path.

## What you do

1. Determine the plugin's hooks directory. Use the `CLAUDE_PLUGIN_ROOT` environment variable if available, otherwise find the `hooks/` directory relative to this skill file.

2. Run the registration command:
```bash
python3 "${PLUGIN_HOOKS}/registry.py" register "$(pwd)"
```

3. Print the result.

If the project is already registered, this is a no-op (idempotent).

## Usage
```
/dynos-work:register
```
