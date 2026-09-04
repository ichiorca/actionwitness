# Roadmap

ActionWitness ships narrow by design. This roadmap separates demonstrated product
behavior from work that remains optional or deferred.

## Shipped in the hackathon build

- Target-neutral outcome contracts and deterministic verdicts.
- Recorded WebMCP tool invocations and independent authoritative observations.
- Workspace and run isolation backed by SQLite migrations.
- Human guidance and server-issued confirmation for protected mutations.
- Append-only, hash-linked evidence and explicit integrity verification.
- Deterministic Buggy Store with pre-fix and post-fix scenarios.
- Portable regression-case generation and replay.
- Imported call-level evaluator reports with source classification, explicit
  binding, repeated-trial correlation, and the dual-layer benchmark matrix.
- Self-witnessing: the harness audited through its own public API.
- The external storefront audit: single asserted origin, browser-side
  collection, sealed and re-verified merchant reports.
- The Shopify development-store integration: pairing, the reviewed theme
  bridge observing the shopper's own cart via `/cart.js`, native Shopify
  WebMCP tools on the agent side, cart-only by contract — exercised end to
  end against the authorized development store.
- Live Gemini intent-variant drafting behind an explicit provider credential,
  with human review before anything is frozen.
- React workspace, browser adapter, public Docker deployment on Render, and
  release gates.

## Near term

- Simplify contract authoring without moving verdict logic into the browser.
- Add more target adapters through the existing public protocols.
- Improve evidence-bundle viewing and portable redacted export.
- Publish a stable packaged CLI and versioned release artifacts.
- Expand real-browser accessibility and cross-browser validation as WebMCP stabilizes.

## Optional modules — implemented, configuration-gated, off by default

All three are built and tested to the same standard as the deterministic demo;
what gates them now is deployment configuration, not pending work. Each reports
its state and reason at `GET /api/v1/workspace` under `modules`.

- **Shopify development-store integration** — enables only for one exact
  authorized store origin, one server-controlled variant, and one currency.
- **Live-model evaluator** — variant drafting and the import pipeline are
  complete and CI-proven from the recorded fixture; the live-credential
  benchmark run itself remains an open operator gate
  (`specs/010-live-model-benchmark/tasks.md` T11).
- **External storefront audit** — enables only for operator-allowlisted
  origins, with a per-audit authorization assertion.

## Explicitly out of scope

- Production checkout, payments, orders, or bulk mutations.
- Automatic target discovery or arbitrary remote execution.
- Hosted multi-tenancy, billing, and organization administration.
- An LLM as the authoritative business-state judge.
- Reimplementation of generic MCP schema or call-selection evaluators.

Scope can expand only through an explicit design decision that preserves validation,
consent, idempotency, source independence, evidence integrity, and workspace isolation.
