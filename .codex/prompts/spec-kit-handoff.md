# Prime spec-kit handoff for $ARGUMENTS

Rewrites `metadata.linkedSpec`, emits one `agent.workflow.spec-kit.task` event
per declared task ID, and tags the spec by git-cleanliness. Used as a pre-flight
before non-autonomous implementation passes.

## Run it

```
cavesson spec-kit handoff --spec=$ARGUMENTS
```

Report the spec's provenance class (`repo-versioned` vs `repo-unversioned`) and
the task count emitted. If `repo-unversioned`, prompt the operator to commit
the spec.md so downstream provenance carriers land cleanly.
