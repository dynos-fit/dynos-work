---
name: register
description: "Internal dynos-work skill. Register the current project directory with the dynos-work global registry. Works from any project."
---

# dynos-work: Register

Registers the current working directory as a dynos-work project in the global registry at `~/.dynos/`.

## What you do

1. Determine the plugin's hooks directory using the configured hooks path ({{HOOKS_PATH}}), falling back to the `hooks/` directory relative to this skill file if unavailable.

2. Run the registration command:
```bash
python3 "{{HOOKS_PATH}}/dynoregistry.py" register "$(pwd)"
```

3. Print the result.

If the project is already registered, this is a no-op (idempotent).

## Usage
```
/dynos-work:register
```
