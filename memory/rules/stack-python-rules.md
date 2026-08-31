---
title: Python coding rules
scope: project
---

**Iron Law: NEVER BREAK PACKAGE ISOLATION OR EVIDENCE DETERMINISM.**

Violating the letter of these rules is violating the spirit of these rules.

- MUST target Python 3.12; use built-in generics, `X | None`, exhaustive enums, and precise annotations on public APIs and `Protocol` members.
- MUST treat this as a `uv` workspace of independent `src/` distributions; declare every import in the importing member’s `pyproject.toml` and NEVER rely on sibling packages leaking through the shared environment.
- MUST keep `actionwitness_core` synchronous, target-neutral, and free of FastAPI, HTTPX, aiosqlite, integrations, demo-commerce types, environment reads, and import-time I/O.
- NEVER add Python-side WebMCP registration or an undeclared WebMCP SDK; browser registration belongs in the TypeScript adapter unless the architecture and dependencies explicitly change.
- MUST validate HTTP bodies, imported reports, persisted records, and adapter responses with explicit Pydantic models; normally forbid unknown fields and use strict fields where coercion could alter identifiers, booleans, counts, hashes, versions, or money.
- NEVER use mutable default arguments or shared mutable model state; use factories and normalize hash-bound nested collections to immutable values—frozen Pydantic models are only shallowly immutable.
- MUST use exact decimal values for money, timezone-aware UTC instants for persisted time, and injected clocks, IDs, and randomness for replayable logic; NEVER use floats, naive datetimes, Python `hash()`, or incidental dict order as evidence.
- MUST model tool-reported output and authoritative observations as distinct types; NEVER manufacture observed state from a successful tool response.
- MUST keep pure evaluation synchronous and place `async` only at I/O seams; reuse lifespan-owned clients, keep strong task ownership, propagate cancellation, and NEVER hold a workspace lock or SQLite transaction across network, browser, or human waits.
- MUST catch the narrowest useful exception, preserve causal context with `raise ... from ...`, and NEVER turn cancellation, indeterminate mutations, or validation failures into success.
- MUST run `uv run pytest -q` and `uv run pytest tests/architecture -q` for Python changes. Ruff is not currently declared or configured: NEVER claim it as a required gate until it is added to project configuration and CI; if adopted, configure Python 3.12 and run formatting and linting as separate checks.

| Excuse | Reality |
|---|---|
| “The import works in my workspace.” | A sibling distribution may be supplying an undeclared dependency. |
| “The tool returned success.” | That channel is evidence, not authoritative state. |
| “Frozen means immutable.” | Nested lists and dictionaries can still change after hashing. |
| “Retrying after timeout is harmless.” | The target may already have committed the mutation. |

Red flags — STOP:

- “Should be deterministic.”
- “The shared environment has it.”
- “I’ll add the boundary test later.”
- “Just retry with a new key.”

<!-- sources fetched at generation: https://docs.astral.sh/ruff/, https://docs.astral.sh/ruff/configuration/, https://docs.astral.sh/ruff/linter/, https://docs.astral.sh/ruff/formatter/ -->
