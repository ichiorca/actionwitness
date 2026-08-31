---
title: Coding standards
scope: project
---

- Prefer many small, cohesive files over few large ones; keep functions short and single-purpose.
- Name for intent: descriptive variables/functions; boolean names read as predicates (is/has/should).
- Favor immutability — return new values instead of mutating shared state where practical.
- KISS / DRY / YAGNI: simplest thing that works, extract real duplication, don't build for imagined futures.
- Validate inputs at system boundaries; never trust external data (user input, API responses, file contents).
- Handle errors explicitly at every layer; never silently swallow them. User-facing messages stay safe.
- Match the surrounding code's style, idioms, and formatter/linter config — don't introduce a personal style.
