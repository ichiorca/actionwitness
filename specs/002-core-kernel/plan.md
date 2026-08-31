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
