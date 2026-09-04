<!-- run the relevant tests for the change (or scaffold missing ones) -->

Run (or author) the tests relevant to: `$ARGUMENTS`

Steps:
1. Detect the project's test runner from its config/manifests (do not assume — read the repo).
2. Identify the tests covering the target file/package. If none exist, scaffold focused tests first (happy path + the key edge cases).
3. Run the narrowest test command that covers the change (a single package/file), not the whole suite, unless asked.
4. If tests fail, report the failure verbatim and the smallest fix; re-run to confirm green.
5. Summarize: what ran, pass/fail, and any coverage gaps worth filling.

Use the project's real commands and conventions. Never weaken an assertion to make a test pass.
