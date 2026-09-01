# 007 — plan

Order of work — core models first, then generation, then replay, then the
surfaces, so every stage lands with tests before anything depends on it:

1. **Core eval models** in `actionwitness_core.evals` — versioned case,
   fixture, trajectory expectation, environment expectation, interaction
   strategy, eval report. Frozen, `extra="forbid"`, schema-versioned, built on
   the kernel's Decimal/UTC/injected-ID discipline. **The evals-lane tripwire
   (`tests/evals/test_evals_lane.py`) fails the moment this lands — replacing
   it with real §24 coverage is part of the same task, per its own docstring.**
2. **Case generation** from a terminal failed/warning run: source-contract-hash
   embedding and verification, safe fixture minimization (retain
   policy-relevant repeated IDs), sensitive-value replacement with
   deterministic fixtures, final-hash-last ordering, idempotency (re-generation
   returns the existing case).
3. **Tier 2 migrations** for the eval tables (§17) — the 004 runner gains an
   ordered migration, never startup table creation.
4. **Eval workspaces** — internal, isolated by the 004 cookie/lock machinery,
   swept by the existing cleanup with built-in templates preserved.
5. **Restore + replay** — fixture restoration through the registered managed
   adapter only; allowlisted calls only; scenario mapping (`current` →
   `post_fix`, `reproduce_source` → immutable source selection) in
   `integrations.buggy_store` / application, never core.
6. **Interaction providers** — recorded approval, recorded denial,
   no-confirmation; a replay may never manufacture consent the source run did
   not contain (constitution: an agent cannot create its own consent — that
   includes a replayed one).
7. **Comparison + report** — overall result and exact critical classification
   set; eval status distinct from business outcome; immutable eval report,
   then mutable eval-target state cleaned.
8. **Surfaces** — API routes, `EvalPanel` (server-state-driven enablement like
   every 006 panel), CLI `eval validate`/`eval run` with JSON report and exit
   codes 0/1/2 (0 pass, 1 expectation failure, 2 invalid definition/harness
   error — the codes the M0 CLI stub already reserves).

Cross-cutting:

- **Determinism is the product here.** Same case + same fixture → byte-identical
  report; injected clock/IDs everywhere; no wall-clock, no network beyond the
  local adapter.
- **The eval must be able to fail.** reproduce_source proving the source
  failure and current proving the fix are different directions of the same
  case; a suite where both always pass is broken by construction.
- **Reuse 004/005, do not fork.** Eval workspaces are workspaces; replay
  dispatch is the 005 invocation path with a different actor; the comparison
  reads the 005 report models.

## Deviations and decisions worth an operator's eye

_To be recorded per task, anchored to spec sections — the 002–006 convention._

### Carried forward, still open

- `maximum_mutations` §22 mapping (002); ADR-0004 integer bound (002); store
  project-allocated endpoints + `X-Workspace-Id` (003); FR-030 reading (005);
  `runs.fault_active` population (005/006); FR-039 lease surface (005/006);
  four §11.1 tools out of 006's T7 scope — two of them
  (`create_regression_eval`, `run_regression_eval`) land HERE as WebMCP tools
  if the operator reads §11.1 as requiring them in this milestone;
  `ComparisonPanel` wiring (006); store-frontend lint (hardening).
