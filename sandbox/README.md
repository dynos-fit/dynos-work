# sandbox/ — MCTS design simulation

Extracted from the dynos-work main pipeline. These modules provide Monte Carlo sandbox simulation for design option scoring.

## What's here

| File | What it does |
|---|---|
| `dream.py` | MCTS-lite playout runner — scores design options via rollout simulation |
| `state.py` | State encoder — produces architecture complexity, dependency flux, finding entropy |
| `state-encoder.md` | Agent spec for the state encoder |
| `founder-skill/` | Skill definition for the founder design-review service |

## Usage

```bash
# Encode current repo state
python3 sandbox/state.py --root . --target src/api/

# Run MCTS on design options
python3 sandbox/dream.py design-options.json --root .
```

## Dependencies

Requires `hooks/` on PYTHONPATH for: `lib_core`, `lib_defaults`, `lib_trajectory`.

## Why extracted

These modules ran only during optional high-risk design review (founder skill). They were never in the critical path (start → execute → audit). Extracted to keep the main pipeline clean while preserving the code for standalone use.
