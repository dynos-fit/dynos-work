## Smallest Complete Change (injected)

Build everything your acceptance criteria require, with the least new code. The spec decides what exists; you decide how little code delivers it.

Before writing new code, take the first option that fully works:

1. Already in this codebase (a helper, component, service or pattern in your files or the references you were given)? Use it the way the surrounding code does.
2. The standard library or a platform feature? Use it, unless the project has its own equivalent.
3. An installed dependency? Use it. Never add a dependency for a few lines. A new dependency the plan did not name goes under Open Questions in your evidence, not into the manifest.
4. Otherwise, the minimum code that works. One line a reader gets at a glance beats ten; a one-liner that needs decoding does not.

- Nothing the criteria do not ask for: no abstraction with a single implementation, wrapper, option, config flag or "for later" code. Keep the layers, interfaces and conventions the codebase already has. Deletion beats addition.
- Small applies to the solution, never to the change: finish every part your segment owns, including tests and fixtures in your files that your change breaks.
- When you change a function's signature, return value or behavior, find its callers with one targeted Grep for its name; that search is inside your read budget. Fix the callers in your `files_expected` and list any others under Open Questions. Do not edit outside your boundary, and do not leave a broken caller unreported.
- Fix a bug once, at its root in the shared code, not in each caller.
- Code you move or merge keeps its validation and error handling.
- Comment only the why the code cannot show.
- A deliberate shortcut with a known limit gets one comment in this form: `shortcut: <the limit>, <when to upgrade>`. Auditors read a marked shortcut as a decision to weigh, not an oversight.

Never cut to make it smaller: validation at trust boundaries, error handling that prevents data loss, security, accessibility, or any state or behavior your criteria name.

End your evidence file with a `## Skipped and Not Checked` section: what you left out on purpose and what you could not verify. Write "None" if there is nothing.

<!-- Adapted from Ponytail (MIT); see THIRD_PARTY_NOTICES.md. -->
