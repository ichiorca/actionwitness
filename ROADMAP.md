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

## Live on the deployed demo

Everything above runs on the public deployment, including the Shopify
development-store integration (enabled against the authorized store) and the
external storefront audit (enabled for the allowlisted origins). Store origin,
variant, currency, and audit origins are server-controlled configuration as a
safety stance, not a feature flag: a deployment without them refuses rather
than guesses, and no request body can override them. The one
credential-dependent piece is live-model variant drafting — it requires a
provider credential in the deployment, and the benchmark view otherwise runs
from the clearly-labelled recorded fixture.

## Explicitly out of scope

- Production checkout, payments, orders, or bulk mutations.
- Automatic target discovery or arbitrary remote execution.
- Hosted multi-tenancy, billing, and organization administration.
- An LLM as the authoritative business-state judge.
- Reimplementation of generic MCP schema or call-selection evaluators.

Scope can expand only through an explicit design decision that preserves validation,
consent, idempotency, source independence, evidence integrity, and workspace isolation.
