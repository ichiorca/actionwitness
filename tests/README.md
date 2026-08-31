# Test layout (spec v1.9 §18, §26)

| Directory | Scope | Spec |
|---|---|---|
| `architecture/` | forbidden-import and layering gates — runs from day one | §26.7 |
| `unit/` | engine operators, policies, hashing, classification | §26.1 |
| `integration/` | run lifecycle, snapshots, confirmation, replay through the app | §26.2 |
| `adapters/` | adapter protocol conformance incl. the non-commerce fake target | §26.7 |
| `contracts/` | contract parsing/validation | §26.1 |
| `evals/` | case factory, replay, CLI exit codes | §26.2 |
| `benchmarks/` | evaluator import, binding, matrix/metrics | §26.5 |
| `guidance/` | human-agent collaboration guidance states | §12.13 |
| `shopify/` | Tier 3 bridge/adapter tests | §26.6 |
| `browser/` | manual smoke checklists only — NOT automated in CI (§7.5 hard cut) | §26.4 |

Run the architecture gates now: `uv run pytest tests/architecture -q`
