<p align="center">
  <a href="https://dynos.fit">
    <img src="dynos-logo-dark.svg" alt="dynos.fit" />
  </a>
</p>

# dynos-work

A Claude Code plugin that builds features, catches its own bugs, and gets better at your codebase over time.

## Install

```
claude plugin marketplace add dynos-fit/dynos-work
claude plugin install dynos-work
```

## Use

```
/dynos-work:start build a user settings page
```

That's it. You'll answer a few questions about what you want, approve a spec, approve a plan — then it builds.

## What happens

When you start a task, the plugin:

1. **Asks what you need** — a few targeted questions to nail down scope
2. **Writes a spec** — exact behavior, edge cases, what's out of scope. You approve it.
3. **Writes a plan** — which files change, in what order, how it'll be tested. You approve it.
4. **Builds in parallel** — multiple agents work on independent pieces simultaneously
5. **Audits its own work** — independent agents check for bugs, security issues, and spec gaps
6. **Repairs what it finds** — automatically, up to 3 attempts before it asks you

After the task, it learns from what went wrong and builds prevention rules for next time. Your 10th task is better than your 1st.

## Commands

```
/dynos-work:start [task]       start a new task
/dynos-work:execute            run the approved plan
/dynos-work:audit              audit, repair, and close
/dynos-work:status             check where your task is
/dynos-work:resume             continue after interruption
/dynos-work:investigate [bug]  deep bug investigation
/dynos-work:residual           inspect or drain non-blocking findings
```

## Requirements

Just Claude Code.

## Links

- [Changelog](CHANGELOG.md)
- [Internals & CLI reference](INTERNALS.md)
- [Architecture](ARCHITECTURE.md)
- [Pipelines](PIPELINES.md)

---

> Completion is a control decision, not a model opinion.
