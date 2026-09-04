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
| `invocation_service.py` | 1229 | Tool dispatch, confirmation gating, redaction, identity checks, event recording | **One of the largest modules in the repo (only `shopify_pairing.py` is bigger) and a known review hotspot.** Split along its existing seams (redaction policy, request identity, tool-spec lookup) before adding to it. A failed observation is recorded as *absent* and logged with a traceback — the verdict is the same for every cause, the operator's response is not. |
| `verification_service.py` | 999 | The verdict: gate → capture → evaluate → seal | Large, but the phase structure is the point. Read it before writing any code that mixes I/O and transactions. |
| `verification_gate.py` | 272 | Claiming a run for verification | Wins the race in its own transaction so two verifications cannot both proceed. `_COMPLETED_ACTIONS` also admits `external_observation_received`: an `external_webmcp` run records no `tool_invocation_*` event by design (§9.1), and §16 names that event as the evidence in its place. The in-flight check is untouched. |
| `run_service.py` | ~570 | Run lifecycle and arming | |
| `self_witness.py` | ~215 | FR-172's observer isolation: minting the observed workspace, the recursion cap, and the two seams (`capture_scoped`, `bound_adapter`) every service reads and acts through | Recognition is by **protocol**, never by target name — an `isinstance` check on `ScopedObservationProvider`/`ScopedTargetAdapter`. `bound_adapter` runs before the agent's arguments are validated, so no tool argument can name the workspace a call lands on. |
| `confirmation_service.py` | 309 | Server-issued consent, bound to workspace/run/action/arguments/expiry | An agent can never create, broaden, or approve its own consent. A stale approval is refused, not honoured. |
| `decision_service.py` | 393 | Human approve/deny handling | |
| `surface_service.py` | 260 | Tool-surface capture and change detection | |
| `timeline_service.py` · `findings_service.py` · `comparison_service.py` | 126 · 205 · 139 | Timeline paging; finding reads; matched pre/post comparison | |
| `guidance_service.py` | 248 | Records and reads guidance state | `current_guidance` is the one place any surface asks "whose turn is it?". It also reads whether the active run has a regression case and an open replay, because §11.5's eval edges leave the run's own state untouched. |

## Shopify pairings (§16.5, Tier 3)

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `shopify_pairing.py` | 1453 | §16.5's ten states and every transition between them; credential minting, the salted digest, expiry, idempotent capture, §23.9's `external_target` provenance block, the integrity-checked `status_document` projection the status endpoint serves, and the atomic finalization callback | **A raw credential exists in exactly two stack frames and no column.** Migration 9 stores only `*_token_hash`, and `PairingView` deliberately carries neither hash, so nothing added to `as_document()` can publish one. The digest covers workspace, contract, and store origin as well as the secret, so a cross-workspace presentation fails on the hash rather than on a `WHERE` clause. Expiry reads `UnitOfWork.instant()`, never the wall clock. `verify` hands `VerificationService` an `on_seal` callback because §16.5 requires the pairing's terminal state and the run's to commit together; a second transaction afterwards would satisfy the words and not the purpose. The integration is imported inside `resolve_shopify_adapter` only (§21.1). |

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
| `benchmark_service.py` | ~1130 | Suites, imports, bindings, sealing, finalization, repetition planning | Large. Finalization is `prepare_finalize` → `write_benchmark_report` → `seal_finalize` so the file write lands between transactions. Nothing here binds a trial automatically — FR-091 forbids guessing a binding. `plan_repetitions` holds every refusal a repeated-trial batch can make, including `MAX_TRIAL_REPETITIONS`; `record_repetition` re-checks `draft` on each insert, because the batch does not hold the workspace lock across its replays. |
| `benchmark_metrics.py` · `benchmark_replay.py` | 116 · ~480 | The quadrant; trajectory replay; repeated-trial batches | `RepeatedTrialService` commits each repetition's row *before* replaying it, so a cancelled batch leaves what it started visible and retries nothing. It reaches into `BenchmarkService`; nothing reaches back, and the dependency must stay one-way. |
| `benchmark_correlation.py` | ~250 | Per-variant evaluator-verdict vs observed-outcome correlation | Pure, synchronous, no database and no clock. Every count and rate comes from `actionwitness_core.benchmarks.matrix`; it adds only the agreement and understated rates via `Rate.of`. There is deliberately no total across populations — §9.9 forbids pooling. |
| `variant_generation.py` | ~180 | FR-100's generate → validate → screen, stopping short of approval; `live_credential` | **It cannot approve, and a test reads the source to prove it.** The proposer is a `Protocol`, so this module names no vendor and holds no HTTP client. Screening runs *before* validation because a credential means rotate and a shape error means regenerate. The credential is read here from an injected mapping and never stored. |
| `eval_case_service.py` | 559 | Case generation and reads | |
| `eval_run_service.py` · `eval_runner.py` | 534 · 486 | Replay execution in an isolated eval workspace | |
| `scenario_service.py` | 139 | Scenario/fault selection | Refuses a fault profile the target cannot inject. |

## Platform-facing

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `adapter_registry.py` | 373 | Target adapter lookup by module name **or** target id; `supported_fault_profiles()`, the read-only companion to `injects_fault_profile` (which stays the gate) | Registers `self`, `buggy_store`, and `shopify` — each imported lazily inside its builder. An unknown target is refused with `TARGET_UNAVAILABLE`, never a crash. |
| `workspaces.py` · `workspace_service.py` | 246 · 363 | Workspace resolution, cookies, creation metering | FR-013's reset also cancels a nonterminal Shopify pairing **and clears its stored bridge-session digest** — the second write is what makes the reset reach a storefront tab that is still open. The pairing states are imported inside the method: `run_service` imports this module and `shopify_pairing` imports `run_service`. |
| `rate_limits.py` | 197 | Per-client token buckets | `release_idle()` must stay wired to the cleanup sweep, or the process keeps one entry per address it has ever seen. |
| `cleanup.py` | 248 | FR-009 sweep: expired workspaces, rows, files | Also runs the process's other periodic maintenance via `on_sweep`. A failed sweep is logged, never silently swallowed. Files are unlinked **after** the rows commit. |
| `authorization.py` | 121 | `WorkspaceScope` — scoped reads | A known id grants nothing; every scoped read carries the workspace term. |
| `contract_service.py` | 405 | Template seeding, instantiate, select | Seeding is idempotent and keyed by content hash, so an edited template becomes a new row rather than rewriting one an armed run points at. |
| `template_catalogue.py` · `artifacts.py` · `guidance.py` · `orchestration.py` | 84 · — · 5 · 4 | Catalogue; (above); re-export shims | The last two are thin re-exports. |
