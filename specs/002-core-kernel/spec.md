# 002 — Target-neutral core kernel (M1)

**Source:** `docs/BUILD_ORDER.md` §7/M1 · functional spec v1.9 §9–10, §16,
§18, §22–23
**Goal:** a framework-neutral library that validates and evaluates an outcome
contract without FastAPI, SQLite, HTTP clients, or Buggy Store present.

## Scope (implementation areas)

- `contracts` — Pydantic models, limits, restricted dotted paths,
  expected-tool semantics, policy/operator enums, immutable contract records,
  schema export.
- `ports` — `TargetDescriptor`, `ScenarioSelection`, `TargetToolSpec`,
  `ExecutionContext`, `ToolExecutionResult`, `Observation`, adapter protocols,
  repository protocols.
- `evidence` + `security` — event/snapshot models, default and contract
  redaction, canonical JSON (RFC 8785, ADR-0004), SHA-256 hashing, bounded
  summaries, resource-limit models.
- `engine` — exact path resolution, the eight assertion operators, severity
  aggregation, expected-tool multiset/subsequence checks, confirmation policy,
  deterministic primary-failure ordering, generic/causal classification.
- `journeys` — closed run/eval/benchmark state enums, pure transition
  validation (persistence of transitions stays in the application layer).
- `reports` — versioned layered outcome report models.

All contract policy types are recognized and safely evaluated from the start;
the Tier 3 label gates when injected demonstrations are *exposed*, never
whether a seeded policy is silently ignored.

## Acceptance criteria / exit gate

1. Core installs and tests in isolation with no application or integration
   package available.
2. Published RFC 8785 vectors and repository fixture vectors pass.
3. The non-commerce in-memory adapter evaluates `target.ticket.status`
   through public protocols.
4. Unknown fields, paths, operators, policy types, schema versions,
   non-finite numbers, and unsafe contract sizes fail with structured errors.
5. Same inputs and hashes produce byte-identical reports.
6. `uv run pytest tests/architecture -q` still passes (no forbidden imports).

## Non-goals

- No persistence, HTTP surface, or Buggy Store semantics (003–004).
- No WebMCP or frontend work.

## Implementation order (normative)

1. closed enums and shared models → 2. path validation/resolution →
3. canonicalization/redaction → 4. assertions and aggregation →
5. trajectory and policy evaluation → 6. classifications and report
composition.
