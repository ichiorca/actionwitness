<!-- stage and write a conventional commit for the current change -->

Stage the current change and write a commit.

Optional message hint: `$ARGUMENTS`

Steps:
1. Run `git status` and `git diff` to see what changed; group related changes.
2. Stage the intended files (skip build artifacts, secrets, unrelated edits).
3. Write a Conventional Commit message: `<type>: <summary>` (feat / fix / refactor / docs / test / chore / perf), with a short body explaining the *why* when non-trivial.
4. Show the staged diff summary and the proposed message, then commit.
5. Do NOT push unless asked.

Keep the summary under ~72 chars, imperative mood. If the change mixes concerns, suggest splitting into multiple commits.
