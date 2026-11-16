# Third-party notices

dynos-work is MIT licensed (see `LICENSE`). Parts of two agent prompts adapt
ideas and wording from other projects. No code from either project is
included, and neither project is required to use dynos-work.

## Hallmark

- Source: https://github.com/Nutlope/hallmark (`skills/hallmark/references/interaction-and-states.md`, `references/gates.md`)
- License: MIT. Copyright (c) 2026 Hallmark contributors.
- Used in: `agents/ui-executor.md` (the Interaction Contract: control states,
  form behavior, overlays, undo over confirm, loading and empty states) and
  `agents/ui-auditor.md` (success feedback with no request behind it).
- Changes: condensed, rewritten for dynos-work's executor and auditor
  contracts, and stripped of Hallmark's theme tokens and gate numbers.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Impeccable

- Source: https://github.com/pbakaus/impeccable (`reference/audit.md`, `reference/craft-floor.md`)
- License: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0). Created by Paul Bakaus.
- Used in: `agents/accessibility-auditor.md` (target-size exceptions, reduced
  motion) and `agents/ui-executor.md` (the visual checks and the list of stock
  defaults to avoid when no design system exists).
- Changes: condensed and rewritten for dynos-work's executor and auditor
  contracts; Impeccable's commands, scoring and detector are not used.
