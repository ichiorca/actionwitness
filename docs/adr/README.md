# Architecture decision records

One record per load-bearing decision, in `NNNN-kebab-title.md`, following
`0000-template.md`. Every record carries context, decision, positive **and**
negative consequences, rejected alternatives, status, date, and the implementing
change (BUILD_ORDER §6).

Accepted records are immutable. A reversal is a new record that supersedes the old
one; the trail of why the project changed its mind is the reason these files exist.

`tests/architecture/test_adr_records.py` enforces the structure, the filename/ID
agreement, and that the `Status` column below matches each record's own `Status`
field. Keep the two in sync in the same commit.

## Docket

The six decisions BUILD_ORDER §6 requires, with the milestone that needs each one
closed.

| ID | Decision | Status | Needed by |
|---|---|---|---|
| [ADR-0001](0001-buggy-store-adapter-transport.md) | Buggy Store adapter transport | Accepted | M2 adapter work |
| ADR-0002 | WebMCP lifecycle package | Not started | M0 — **operator-gated** |
| ADR-0003 | SQLite transaction and lock model | Not started | M3 repositories |
| ADR-0004 | RFC 8785 canonicalization implementation | Not started | M1 immutable records |
| ADR-0005 | External evaluator version and binding | Not started | M7 |
| ADR-0006 | Deployment composition | Not started | M8 |

ADR-0001 through ADR-0004 are the M0 preflight set (`specs/001-preflight-baseline`).
ADR-0005 and ADR-0006 belong to later milestones and are listed so the docket is
complete, not because M0 owes them.

## Statuses

| Status | Meaning |
|---|---|
| `Not started` | Listed in the docket; no record file exists yet |
| `Proposed` | Record exists; the decision is not yet binding |
| `Accepted` | Binding on the codebase; immutable |
| `Superseded` | Replaced by a later record, which it names |

## ADR-0002 is operator-gated

ADR-0002 selects between `use-webmcp-tool` and `usewebmcp` and cannot be closed
from a terminal: it requires a human running the spike harness against the exact
target Chrome/ChatGPT build (spec §25.1, §33 open question 2). An agent session
prepares the harness and the checklist and records the results; the pin itself,
and the frontend lockfile that follows from it, are the operator's decision.
