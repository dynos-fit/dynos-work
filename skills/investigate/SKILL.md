---
name: investigate
description: "Deep bug investigation. Pass a short description of the problem — error message, unexpected behavior, or failing test. Returns structured root cause analysis with evidence and fix recommendation."
---

# dynos-work: investigate

Spawn the `investigator` agent with the user's prompt as the instruction.

## Ruthlessness Standard

- The investigator must name the mechanism, not restate the symptom.
- Every conclusion needs concrete file/function/condition evidence.
- If the evidence does not support a claim, the agent must say so instead of guessing.

## What to pass

Pass the user's full prompt verbatim. Do not summarize or sanitize it.
Prepend a short hard wrapper that tells the agent to trace root cause, immediate cause, and detection failure with evidence.

## Usage

```
/dynos-work:investigate <your problem description>
```

Examples:
```
/dynos-work:investigate TypeError: Cannot read properties of undefined reading 'id' at UserService.ts:47
/dynos-work:investigate the checkout flow always skips the discount calculation when coupon code is applied
/dynos-work:investigate test suite: AuthController > login > should return 401 on invalid password is failing
```
