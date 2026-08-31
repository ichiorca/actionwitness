# 002 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not
implemented).

1. **Enums + shared models** — start from 001-T7's registry; add money as
   `Decimal` (never float), timezone-aware UTC instants, frozen Pydantic
   models with `extra="forbid"`. Injected clock/ID/randomness protocols live
   here so everything downstream is replayable.
2. **Path validation/resolution** — restricted dotted paths as a parsed,
   validated type; exact resolution over observation payloads; structured
   errors for unknown/forbidden segments. Property-style tests over hostile
   path inputs.
3. **Canonicalization + redaction** — RFC 8785 per ADR-0004 against the
   vectors committed in 001-T4; redaction runs BEFORE hashing/persistence;
   non-finite numbers rejected; unordered collections normalized
   deterministically (constitution §4).
4. **Assertions + aggregation** — the eight operators as pure functions over
   resolved values; severity aggregation; deterministic primary-failure
   ordering (tie-break rules unit-tested explicitly).
5. **Trajectory + policy evaluation** — expected-tool multiset/subsequence
   checks, confirmation policy, every policy type recognized (evaluate or
   explicit `not_evaluated` — never silently ignored).
6. **Classification + reports** — generic/causal classification including
   `false_success_or_state_mismatch`; layered report models
   (model-selection / observed-trajectory / execution / business-outcome /
   safety-policy kept distinct, BUILD_ORDER invariant 10); byte-identical
   report serialization test (same inputs → same bytes).
7. **In-memory non-commerce adapter** (`target.ticket.status`) as a test
   fixture proving the ports are sufficient without commerce semantics —
   this is also the AC-19 evidence seed.

Cross-cutting:

- Everything synchronous (constitution: core stays sync; async only at I/O
  seams, which core has none of).
- Isolation proof: a tox-style or uv-venv test job that installs ONLY
  `packages/actionwitness_core` and runs its suite (wire into the arch lane).
- Tool-reported output and authoritative observations are distinct types from
  the first commit; no constructor may build an observation from a tool
  response.

## Status — 002 complete (2026-08-31)

All thirteen tasks landed in the spec's normative order, each with its tests.
`uv run pytest -q` is green (884 tests) and `uv run pytest tests/architecture -q`
is green (60), which now includes the core-only install job.

Exit gate, item by item:

1. **Core installs and tests in isolation.** `scripts/core_only_isolation.py`
   builds a clean venv, installs only `actionwitness_core` plus pytest and
   pytest-asyncio, proves the service, demo and framework packages are absent,
   and runs the 668 core-only tests there. Wired into the architecture lane.
2. **RFC 8785 vectors pass.** All eight accept vectors (published + repository)
   and all three reject vectors from 001-T4.
3. **The non-commerce adapter evaluates `target.ticket.status`.** A full journey
   through the public protocols in `tests/adapters/test_non_commerce_adapter.py`.
4. **Unknown input fails with structured errors.** Fields, paths, operators,
   policy types, schema versions, non-finite numbers, and oversized contracts.
5. **Byte-identical reports.** Asserted over canonical bytes, including
   independence from the order findings were evaluated in.
6. **Architecture lane green.**

### Deviations and decisions worth an operator's eye

- **ADR-0004's integer bound contradicts its own rationale and the corpus.** The
  record says "integers outside ±(2^53 − 1) are rejected" and gives the reason
  "a larger integer cannot round-trip". 2^53 *does* round-trip, and the committed
  corpus contains it as the `two_pow_53` accept vector. The literal bound would
  fail a vector T4 requires to pass, so the implementation follows the stated
  reason — reject what cannot be represented exactly as a double — which still
  rejects 2^53 + 1 and every lossy integer. See
  `_check_integer_is_representable`. **A superseding ADR should correct the
  constant, or the corpus vector should be removed; they cannot both stand.**
- **`maximum_mutations` has no §22 classification.** Five of the six policies map
  onto a published classification exactly. Exceeding a mutation cap maps onto
  none, and inventing a thirteenth would break the exact classification-set
  comparison eval expectations depend on (§24.1, AC-15). It is currently reported
  as `idempotency_violation` — "state changed more times than permitted" — and
  flagged in `engine/policies.py`. FR-064 is Tier 3, so nothing exercises this
  before M11; **the mapping needs an operator decision before then.**
- **`EventActor.EVAL` added to the 001-T7 registry.** §10.3 ("actor `eval` in an
  eval replay") and §23.1 both name it normatively; §17.1's parenthetical list of
  the outcome stream's actors omits it because eval events live in the separate
  `evaluation_events` stream. Registered so the trajectory engine recognises a
  replayed occurrence instead of a replay masquerading as an agent.
- **`SurfaceDeltaKind` added to the registry.** `stable_tool_surface` strictness
  selects from the five §9.5 delta kinds; the policy could not be configured
  without the vocabulary. Detecting the deltas remains later work.
- **Some §23.1 report blocks are deliberately absent.** `tool_surface`,
  `annotations`, and `authorship` have no producer in M1. Modelling a block that
  only ever serialises empty would put a shape into the hashed document before
  its producer is designed, so they join under a `schema_version` bump with the
  milestone that fills them. `undeclared_changes` *is* modelled, because the
  `no_undeclared_changes` policy produces it.
- **`no_undeclared_changes` reports `not_evaluated` without a full-state diff.**
  The core owns the §9.10 declared/undeclared partition; computing the changed
  path set is FR-157, which no milestone has delivered. The policy therefore
  states that it could not be evaluated rather than passing — which is the rule
  BUILD_ORDER §7/M1 exists to enforce.
- **`stable_tool_surface` fails closed with no baseline.** §16.1 requires
  `observation_unavailable` there, so any contract carrying that policy is
  currently unresolved rather than passing. That is the specified behaviour, not
  a gap, but it will look like one until tool-surface capture ships.
- **The shared kernel is `actionwitness_core/kernel.py`, a module rather than a
  subpackage.** §18 fixes the subpackage list; the base every subpackage inherits
  belongs beside them, not inside one.
- **Registry vocabulary was split across modules.** The assertion operators live
  in `contracts/`, the classifications in `engine/`, and so on, composed by the
  new `actionwitness_core.registry`. The drift gate now walks every registry
  module rather than one file, which strengthens it.
- **ADR-0004's own follow-up remains open.** It asks for the upstream
  `cyberphone/json-canonicalization` test data to be run against the
  implementation before M1 closes, noting that vendoring it needs a license check
  first. Not done: it needs a network fetch and a license decision, which is an
  operator call. The committed corpus is green and is the declared floor.
