# Run cavesson evals

Run `cavesson eval run <set>` with your shell tool (e.g. `project-acceptance`, `gdpr-compliance`). To (re)generate evals from the PRD first, run `cavesson eval bootstrap --prd specs/PRD.md`. Report pass/fail per case and surface any CRITICAL/HIGH failures — those block deploy. Never weaken a grader just to make it pass; fix the code or escalate.
