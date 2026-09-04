# core — `packages/actionwitness_core`

> **Paths below are relative to** `packages/actionwitness_core/src/actionwitness_core`.

The assurance library. **Pure, synchronous, target-neutral.** No FastAPI, no
HTTPX, no aiosqlite, no `os.environ`, no integration or commerce import — and not
by convention: `tests/architecture/test_import_boundaries.py` walks the AST of
every file here and fails on a forbidden import root.

If you are adding I/O, an `async def`, or the word "cart" to this package, you
are in the wrong package.

**Start at** `kernel.py` (shared types) and `ports/__init__.py` (the protocols
everything else is written against).

## Foundations

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `kernel.py` | 320 | `Money`, `UtcInstant`, `CoreError`, `JsonValue`, `parse_decimal` | `parse_decimal` rejects `float` **and** `bool`. `bool` is a subclass of `int`, so `True` would otherwise pass as a quantity. Money is `Decimal` everywhere; a float in a money path is a bug, not a rounding choice. |
| `registry.py` | 87 | Adapter/target registration primitives | Target-neutral by construction — it registers, it does not know what it registered. |
| `security/canonical.py` | 257 | RFC 8785 canonical serialization, `content_hash`, `document_content_hash` | The identity of every artifact. Re-serializing a document elsewhere and hashing that is how two "equal" documents get different hashes. |
| `security/redaction.py` | 216 | `RedactionPolicy`, `redact` | Applied before anything is persisted or logged. |
| `security/limits.py` | 126 | Size and depth bounds on untrusted documents | |

## Ports — the seams

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `ports/__init__.py` | 272 | `TargetAdapter`, `ManagedTargetAdapter`, `ExternalTargetAdapter`, `ScenarioReportingAdapter`, `ObservationProvider`, repository protocols | `ObservationProvider` is deliberately **unrelated** to the execution protocols, so no adapter can satisfy an observation by handing back what a tool said. Repositories have no `update` method — append-only is a shape, not a rule. |
| `ports/models.py` | 323 | `ToolExecutionResult`, `Observation`, `TargetToolSpec`, `TargetDescriptor` | The two-channel split lives here. A self-report and an observation are different types on purpose; making one assignable to the other would dissolve the product. |
| `ports/enums.py` | 124 | `RetrySemantics`, `SideEffectClass` | |
| `ports/schemas.py` | 185 | Tool input-schema validation | |

## Contracts and evaluation

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `contracts/models.py` | 605 | `OutcomeContract`, `ContractRecord`, `parse_contract` | Large. `ContractRecord.of` is the single construction path, so the document that is stored and the document that is hashed are the same document. |
| `contracts/paths.py` | 225 | `target.cart.total`-style path resolution | |
| `contracts/enums.py` · `contracts/limits.py` | 196 · 57 | Vocabulary and bounds | |
| `engine/policies.py` | 927 | Policy operators — the assertion vocabulary | **Largest file in core.** Split by operator family if you are adding several. |
| `engine/assertions.py` | 291 | Assertion evaluation | |
| `engine/classification.py` | 288 | Findings → classifications | |
| `engine/enums.py` | 142 | `FailureClassification` — the twelve (§22) | `observation_unavailable` is not a flavour of pass. Collapsing it into one is the exact failure the product exists to prevent. |
| `engine/diff.py` | 397 | State diffing, undeclared-change detection | |
| `engine/findings.py` · `engine/trajectory.py` | 151 · 162 | Finding models; call-order checks | |

## Journeys, evidence, reports

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `journeys/enums.py` | 552 | `RunState` (12 states), `WorkspacePhase` (15), `GuidanceActionCode` (11), `OutcomeEventType`, `EventActor`, `WorkspaceKind` | The event-type vocabulary is closed. A new event type is a schema change, not a string. `GUIDANCE_ACTION_DESCRIPTIONS` is the *only* copy for an action code — the banner reads it through the generated registry rather than holding its own. |
| `journeys/transitions.py` | 220 | `validate_run_transition` | Every state change goes through here. An invalid transition is a refusal, never a row. |
| `journeys/guidance.py` | 454 | Phase → guidance ("who acts next"), and `phase_for`'s projection | Server-owned, so the human and the agent read the same answer. `phase_for` needs the two regression-eval flags to reach `eval_ready`/`eval_running`: §11.5 draws those edges out of a case being created and a replay starting, neither of which changes the source run's state. A `_GUIDANCE` entry no `phase_for` input can produce is dead copy — `test_every_phase_the_projection_can_reach_has_guidance` is the gate. |
| `evidence/surface.py` | 364 | Tool-surface capture, hashing, and diffing (`getTools()` evidence) | Where an undeclared tool-surface change becomes `tool_surface_mutation`. |
| `evidence/effects.py` | 241 | Effect/consequence modelling | |
| `evidence/models.py` · `evidence/enums.py` | 191 · 144 | Evidence records | Append-only, hash-linked, verified before trusted (constitution §4). |
| `reports/models.py` | 660 | Outcome-report composition | |
| `reports/comparison.py` | 234 | Matched pre-fix/post-fix comparison | |
| `reports/enums.py` | 142 | Report vocabulary | |

## Evals and benchmarks

| File | Lines | Owns | Watch for |
|---|---|---|---|
| `evals/models.py` | 607 | `RegressionEvalCase` and friends | Cases are self-contained and versioned; a case must replay from its own bytes. |
| `evals/factory.py` · `evals/minimize.py` · `evals/substitution.py` | 166 · 139 · 91 | Case generation, shrinking, value substitution | |
| `evals/interaction.py` | 88 | FR-087's deterministic interaction providers (`recorded_approval`, `recorded_denial`, `no_confirmation`) | A replay **never infers consent**: a decision the recording lacks fails closed. |
| `evals/enums.py` · `evals/schema.py` | 149 · 89 | Eval vocabulary; the published case schema | |
| `benchmarks/intents.py` | 174 | FR-100 candidate intent variants | Six is a ceiling: an over-sized set is **refused, not truncated** (truncation would silently pick what a human then approves); control characters are refused, not stripped. |
| `benchmarks/models.py` | 511 | `BenchmarkReport`, `BenchmarkManifest`, `NormalizedTrial` | Report validators refuse a mixed correlation-mode population — the two populations may never be aggregated into one rate (§9.9). |
| `benchmarks/states.py` | 90 | `require_transition` for suite status | |
| `benchmarks/enums.py` | 342 | Status, correlation mode, eligibility, exclusion reasons | |
| `benchmarks/matrix.py` · `benchmarks/screening.py` · `benchmarks/approval.py` | 167 · 217 · 247 | The pass×fail quadrant; credential screening; variant freezing | |
