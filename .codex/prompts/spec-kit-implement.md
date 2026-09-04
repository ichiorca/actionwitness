# Implement spec $ARGUMENTS

This is the native-launch counterpart of `cavesson spec-kit implement --spec=$ARGUMENTS`.
The cavesson's L0 broker, L1 event log, and cost telemetry are already armed via the
installed hooks — they fire whether the session was spawned by `cavesson` or launched
natively. This command's job is to drive the spec's implementation; the post-pass
gates (review + completeness + autocommit + autotag) run automatically when the
session ends, via the Stop hook.

## The active spec is already marked — do not write it yourself

Cavesson recorded `$ARGUMENTS` as the active spec when this command was submitted, from
its `UserPromptSubmit` hook. You do **not** need to create
`.cavesson/state/active-spec`, and you cannot: that path is a protected path, so a
write to it is denied before it reaches the filesystem. A denial there is expected
and is not a problem to work around — the marker exists. (An earlier version of
this command asked the agent to write it, which could never succeed and left the
post-pass gates silently disarmed.)

## Read the canonical inputs

1. `specs/$ARGUMENTS/spec.md` — the WHAT.
2. `specs/$ARGUMENTS/plan.md` — the HOW (module layout, key decisions, touchpoints).
3. `specs/$ARGUMENTS/tasks.md` — the discrete T1-Tn tasks. Each is one commit.
4. `memory/constitution.md` — the project's load-bearing invariants (P1-Pn).
5. PRD sections cited under `> PRD anchors:` in the spec.md — read these from
   `docs/PRD.md` (or wherever metadata.prdFile points).
6. `progress/pass*.log` — prior-pass citations. When the current spec is being
   resumed (some tasks already complete), this is the source of truth for what's
   left.
7. `metadata.architecture` in the cavesson manifest (cavesson.cue or
   cavesson.json — whichever this project has) — declared import-edge rules
   and private-access policy.

## Canonical anti-pattern rules (DO NOT SHIP)

1. **No stub migrations.** Every `.sql` file you write contains real DDL/DML.
2. **No unused parameters.** Don't add `# noqa: ARG001` to silence the lint —
   either use the parameter or remove it.
3. **No unreachable modules.** Every new file must be imported by an entry-point
   (CLI handler, scheduler, dispatcher, or test) — not orphan code.
4. **No AC-tests that bypass the entry point.** If spec.md cites a test for an
   acceptance criterion, the test must exercise the same public surface the
   operator invokes — not a private helper.
5. **No undersized fixtures.** When the PRD declares `>= N rows`, the fixture
   you cite must meet N, not approximate it.
6. **Cite the T-ID and Pn in code.** Every meaningful file you touch should
   include a `# Tn (spec NNN) — what this implements` comment and reference the
   constitution principle it honors (e.g. `# P5 (Safety rails):`).

## Do the work

Walk `tasks.md` top to bottom. For each `- [ ] **Tn**` task:

1. Read the task's body — title, dependencies, file paths, tests.
2. Implement it. Use `Write` / `Edit` / `Bash` as needed. The L0 broker enforces
   protected paths, blast radius, and destructive-action policies; if it denies
   a write, the path was likely declared `metadata.protectedPaths` —
   re-read the spec before forcing it.
3. Run the matching test (or run the full project test suite per
   `metadata.testCommand`). Fix until green.
4. Update `tasks.md`: change `- [ ]` to `- [x]` for the completed task.
5. Cite the T-ID in a relevant file (the implementation file's docstring or a
   test's docstring counts).

## Write the progress log

Find the next available `progress/passN.log` slot (sequential N). Write one line
per task you completed in this session, citing the T-ID. This is what the
`cavesson spec-kit review` walker reads to confirm coverage.

## When you're done

Exit cleanly. The Stop hook will:

- Run `cavesson spec-kit review` (9-axis cross-cutting audit).
- Run `cavesson spec-kit completeness` (HC1-HC5 detectors).
- Run `metadata.testCommand` if declared.
- If all green: stage everything, commit with `spec-NNN: T1, T2, ...` message,
  tag `spec-NNN-complete`, clear the active-spec marker.
- If any gate fails: leave the marker in place, surface the findings, and refuse
  to commit. The operator can re-launch and you'll see the marker is still set,
  so resume from the failing task.

**Do not commit or tag yourself.** The Stop hook owns the gate. If you commit
by hand, the gate runs against an already-committed tree and can't roll back on
failure.
