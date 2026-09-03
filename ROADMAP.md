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
- Imported call-level evaluator reports with source classification and correlation.
- React workspace, browser adapter, public Docker deployment, and release gates.

## Near term

- Simplify contract authoring without moving verdict logic into the browser.
- Add more target adapters through the existing public protocols.
- Improve evidence-bundle viewing and portable redacted export.
- Publish a stable packaged CLI and versioned release artifacts.
- Expand real-browser accessibility and cross-browser validation as WebMCP stabilizes.

## Optional modules that remain gated

- Shopify development-store, single-variant, cart-only integration.
- Live-model evaluator runs with explicit provider credentials and recorded provenance.
- External storefront audit for operator-allowlisted origins.

These modules turn on only after their boundaries, recovery behavior, and tests meet
the same standard as the deterministic demo.

## Explicitly out of scope

- Production checkout, payments, orders, or bulk mutations.
- Automatic target discovery or arbitrary remote execution.
- Hosted multi-tenancy, billing, and organization administration.
- An LLM as the authoritative business-state judge.
- Reimplementation of generic MCP schema or call-selection evaluators.

Scope can expand only through an explicit design decision that preserves validation,
consent, idempotency, source independence, evidence integrity, and workspace isolation.
