# Run spec-kit review

Cross-cutting audit across all specs. Reports findings on nine axes:

- `task-coverage` — task IDs in `tasks.md` with no `progress/pass*.log` citation.
- `task-traceability` — task IDs declared but never appearing in `src/`.
- `constitution-coverage` — `Pn.` principles in `memory/constitution.md` never cited.
- `spec-provenance` — spec.md files not cleanly committed.
- `architecture` — forbidden imports + private-access violations.
- `prd-coverage` — PRD requirements under declared anchors with no spec/tasks/decision reference.
- `prd-revision` — REVISION CRITICAL/MAJOR blocks in the PRD never acknowledged.
- `wiring-sites` — production-class symbols defined but unreached from entry-points.
- `param-callers` — public-function parameters with non-trivial defaults no caller passes.

## Run it

Use Bash to invoke:

```
cavesson spec-kit review
```

The command writes `progress/review.json` + `progress/review.md` and emits one
`agent.workflow.spec-kit.review` event on the L1 log. Read `progress/review.md`
and summarize the findings. For each finding, recommend the smallest concrete
change that would close it.

Pass `--fail-on-finding` if you want a non-zero exit to gate a downstream step.
