# service · application — `apps/actionwitness_service/src/actionwitness_service/application`

The use-case layer: orchestration, consent, evidence, and everything that knows
*when* to do something. Routes call in; this layer owns the SQL and the
transaction boundaries.

**Two rules govern almost every file here.**

1. **ADR-0003** — no database transaction may span I/O. `BEGIN IMMEDIATE` is
   SQLite's single writer for the *whole* database, so a file write or an HTTP
   call inside one stalls every other workspace. `verification_service.py` is the
   reference implementation of the correct shape.
2. **Constitution §5** — a tool's self-report is never promoted to an
   observation, and an observation that could not be taken is an explicit
   non-pass, never a quiet pass.

## The run path

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `invocation_service.py` | 1221 | Tool dispatch, confirmation gating, redaction, identity checks, event recording | **Largest module in the repo and a known review hotspot.** Split along its existing seams (redaction policy, request identity, tool-spec lookup) before adding to it. A failed observation is recorded as *absent* and logged with a traceback — the verdict is the same for every cause, the operator's response is not. |
| `verification_service.py` | 927 | The verdict: gate → capture → evaluate → seal | Large, but the phase structure is the point. Read it before writing any code that mixes I/O and transactions. |
| `verification_gate.py` | 239 | Claiming a run for verification | Wins the race in its own transaction so two verifications cannot both proceed. |
| `run_service.py` | 540 | Run lifecycle and arming | |
| `confirmation_service.py` | 309 | Server-issued consent, bound to workspace/run/action/arguments/expiry | An agent can never create, broaden, or approve its own consent. A stale approval is refused, not honoured. |
| `decision_service.py` | 393 | Human approve/deny handling | |
| `surface_service.py` | 260 | Tool-surface capture and change detection | |
| `timeline_service.py` · `findings_service.py` · `comparison_service.py` | 126 · 205 · 139 | Timeline paging; finding reads; matched pre/post comparison | |
| `guidance_service.py` | 202 | Records and reads guidance state | |

## Evidence storage

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `artifacts.py` | 314 | `ArtifactStore`: write, record, read, verify | Paths are **content-addressed** (`<type>-<digest>.json`) and writes are atomic (temp → `fsync` → `os.replace`). `verified_document()` is the single integrity check — four conditions, and every route that serves an artifact goes through it. See ADR-0007. |
| `report_service.py` | 109 | Outcome-report read-back | Delegates verification to `ArtifactStore`; owns only the refusal wording, which names neither path nor hash. |
| `limits.py` | 316 | FR-008 workspace ceilings | Counted inside the committing transaction — a guard that ran earlier would miss concurrent creations. |

## Audits (§12.17)

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `audit_service.py` | 412 | Authorization, lifecycle (`require_live`, `complete`, `cancel`), origin matching | One live audit per workspace, enforced by a partial unique index. Completing or cancelling releases the slot; nothing else does, short of the 24-hour sweep. |
| `audit_workflow.py` | 169 | One audit pass: pack check → normalize → classify → compose | Pure — no I/O, no clock, no database. The pack is the operator's explicit choice (FR-161); this module validates the choice and never makes it. |
| `audit_evidence.py` | 198 | `audit_findings` — the classifier | Order of checks is the meaning. `unobserved` is not a verdict about a tool. |
| `audit_report.py` | 199 | The merchant-readable report (§5 persona) | Keys are shop-owner vocabulary (`audited_site`, `what_to_do`); harness terms stay in `evidence`. |

`tests/architecture/test_audit_guardrails.py` fails the build if any audit module
imports a network client. The server never contacts the audited origin.

## Benchmarks and evals

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `benchmark_service.py` | 914 | Suites, imports, bindings, sealing, finalization | Large. Finalization is `prepare_finalize` → `write_benchmark_report` → `seal_finalize` so the file write lands between transactions. Nothing here binds a trial automatically — FR-091 forbids guessing a binding. |
| `benchmark_metrics.py` · `benchmark_replay.py` | 116 · 331 | The quadrant; trajectory replay | |
| `eval_case_service.py` | 559 | Case generation and reads | |
| `eval_run_service.py` · `eval_runner.py` | 534 · 486 | Replay execution in an isolated eval workspace | |
| `scenario_service.py` | 139 | Scenario/fault selection | Refuses a fault profile the target cannot inject. |

## Platform-facing

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `adapter_registry.py` | 309 | Target adapter lookup by module name **or** target id; `supported_fault_profiles()`, the read-only companion to `injects_fault_profile` (which stays the gate) | Registers `buggy_store` only. An unknown target resolves to `None` → `TARGET_UNAVAILABLE`, never a crash. |
| `workspaces.py` · `workspace_service.py` | 216 · 300 | Workspace resolution, cookies, creation metering | |
| `rate_limits.py` | 197 | Per-client token buckets | `release_idle()` must stay wired to the cleanup sweep, or the process keeps one entry per address it has ever seen. |
| `cleanup.py` | 248 | FR-009 sweep: expired workspaces, rows, files | Also runs the process's other periodic maintenance via `on_sweep`. A failed sweep is logged, never silently swallowed. Files are unlinked **after** the rows commit. |
| `authorization.py` | 121 | `WorkspaceScope` — scoped reads | A known id grants nothing; every scoped read carries the workspace term. |
| `contract_service.py` | 405 | Template seeding, instantiate, select | Seeding is idempotent and keyed by content hash, so an edited template becomes a new row rather than rewriting one an armed run points at. |
| `template_catalogue.py` · `artifacts.py` · `guidance.py` · `orchestration.py` | 84 · — · 5 · 4 | Catalogue; (above); re-export shims | The last two are thin re-exports. |
