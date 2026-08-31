# ADR-0000 — Template

- **Status:** Proposed
- **Date:** 2026-08-31
- **Implementing change:** _commit or task ID, once the decision lands_

## Context

What forces make this a decision rather than a detail? Name the constraint that
rules out the obvious option — a constitutional invariant, an architecture gate,
an external dependency, a deadline. Cite the specification section or the
`memory/constitution.md` rail that binds the choice.

## Decision

One decision, stated in the present tense as a rule the codebase follows. Name
the module or boundary that owns it.

## Consequences

### Positive

- What this buys, concretely.

### Negative

- What it costs, and what it makes harder later. A record with no negatives is
  marketing, not a decision. Name the follow-up work the cost implies.

## Rejected alternatives

### <Option>

Why it was rejected. Prefer the reason that would still be true in a year.

## Notes

Optional: verification evidence, links, and the conditions that would justify a
superseding record.

---

**Copying this template:** number the file `NNNN-kebab-title.md`, add a row to
`docs/adr/README.md`, and keep the index `Status` column identical to the record's
own `Status` field — `tests/architecture/test_adr_records.py` enforces both.
Accepted records are never edited in place; reverse one with a new superseding ADR.
