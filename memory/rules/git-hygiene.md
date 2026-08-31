---
title: Git hygiene
scope: project
---

**Iron Law: NOTHING REACHES SHARED HISTORY WITHOUT BEING ASKED FOR AND REVIEWED.**

Violating the letter of these rules is violating the spirit of these rules.

- Write Conventional Commits: `<type>: <summary>` (feat/fix/refactor/docs/test/chore/perf), imperative, ≤72 chars.
- Keep commits focused — one logical change each; split mixed concerns into separate commits.
- Never commit secrets, credentials, build artifacts, or unrelated edits. Review `git diff` before staging.
- Only commit or push when explicitly asked; do not amend or force-push shared history without instruction.
- Branch off the default branch for new work; keep the branch up to date before opening a PR.
- PR descriptions summarize the *why* and include a test plan.

| Excuse | Reality |
| --- | --- |
| "It's a tiny fix, committing saves a round-trip" | Unasked commits take work out of the operator's hands. Ask. |
| "Amending keeps history clean" | Amending shared history rewrites what others built on. New commit. |
| "The diff is obviously fine, no need to review it" | Secrets and stray files ship in "obviously fine" diffs. Review `git diff` first. |
| "`--no-verify` just this once — the hook is slow" | Hooks are the project's gate. If a hook fails, fix the cause. |

Red flags — STOP: "just this once", "I'll clean up history after", "the hook is probably wrong", committing without being asked.
