# Close milestone $ARGUMENTS

Run the milestone closer and report what it found:

```bash
cavesson spec-kit milestone-close --milestone=$ARGUMENTS
```

## What the result means

The closer compares what the specs promised against what actually shipped. A
**deviation** is a difference it found. The close FAILS while any deviation is
unapproved — that refusal is the feature, not an error to route around.

## When it refuses

Report each unapproved deviation to the operator in plain terms: what was
promised, what shipped, and which spec and task it traces to. Then stop and let
them decide. Two outcomes are theirs, not yours:

1. **The deviation is acceptable** — the operator records an approval under
   `.cavesson/state/milestone-approvals/`. You cannot write that file: it is a
   protected path, and a write is denied before it reaches disk. Approving your
   own deviation would defeat the gate, which is exactly why the path is closed
   to you.
2. **The deviation is a defect** — fix the code so the promise holds, then
   re-run the close.

Never edit a spec to match what shipped in order to make a deviation disappear.
That converts a caught divergence into a silent one and is the single worst
thing this command can be used to do.
