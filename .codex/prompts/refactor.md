<!-- refactor the target for clarity without changing behavior; keep tests green -->

Refactor the target for clarity, without changing behavior: `$ARGUMENTS`

Steps:
1. Read the target and its tests. If behavior isn't covered by tests, add characterization tests FIRST so the refactor is verifiable.
2. Identify the smells: long functions, deep nesting, duplication, unclear names, leaky abstractions.
3. Make small, behavior-preserving changes — extract functions, rename for intent, collapse duplication, simplify control flow.
4. Run the tests after each meaningful step; keep them green throughout.
5. Summarize what changed and why it's clearer; confirm no behavior changed.

Do not add features or change public APIs unless explicitly asked. Smaller, safe steps over a big rewrite.
