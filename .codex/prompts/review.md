<!-- review the working changes (or named path) for bugs, security, and style -->

Review the code changes for correctness, security, and style.

Target: `$ARGUMENTS` (a path, a PR reference, or empty — if empty, review the current uncommitted working changes).

Steps:
1. Determine the diff to review (uncommitted changes, the named path, or the referenced PR).
2. Read the changed files and enough surrounding code to judge them in context.
3. Report findings grouped by severity — **CRITICAL** (block), **HIGH**, **MEDIUM**, **LOW** — each with file:line and a concrete fix.
4. Check: bugs and edge cases, input validation, auth/secrets handling, error handling, test coverage for the change, and naming/cohesion.
5. End with a one-line verdict (approve / needs-changes) and the top 3 things to fix first.

Be specific and cite `file:line`. Do not rewrite the code unless asked — surface the issues.
