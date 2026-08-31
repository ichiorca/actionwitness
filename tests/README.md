# Test layout (spec v1.9 §18, §26)

| Directory | Marker | Scope | Spec |
|---|---|---|---|
| `architecture/` | `architecture` | forbidden-import, layering, docs, lane, and command-surface gates | §26.7 |
| `unit/` | `unit` | engine operators, policies, hashing, canonicalization, classification | §26.1 |
| `integration/` | `integration` | run lifecycle, snapshots, confirmation, replay through the app | §26.2 |
| `adapters/` | `adapters` | adapter protocol conformance incl. the non-commerce fake target | §26.7 |
| `contracts/` | `contracts` | contract parsing/validation and limits | §26.1 |
| `evals/` | `evals` | case factory, replay, CLI exit codes | §26.2 |
| `benchmarks/` | `benchmarks` | evaluator import, binding, matrix/metrics | §26.5 |
| `guidance/` | `guidance` | human-agent collaboration guidance states | §12.13 |
| `shopify/` | `shopify` | Tier 3 bridge/adapter tests | §26.6 |
| `browser/` | `browser` | manual smoke checklists only — NOT automated in CI (§7.5 hard cut) | §26.4 |

    uv run pytest -q                      # everything
    uv run pytest tests/architecture -q   # architecture gates
    uv run pytest -q -m guidance          # one lane

Markers are registered in the root `pyproject.toml` under `--strict-markers`, so
an unregistered marker is an error rather than a silently unselected test.
`tests/architecture/test_test_lanes.py` fails if a lane has no directory, has no
registered marker, or selects zero tests — an empty lane passes vacuously, which
is the failure worth catching.

## Fixture builders (`conftest.py`)

Determinism is constitutional, not stylistic: injected clocks, identifiers, and
randomness are what make evaluation and replay reproducible. These exist before
the product code so no later path has an excuse to call `datetime.now()` or
`uuid4()` inside something that must replay identically.

| Fixture | Provides |
|---|---|
| `frozen_clock` | A UTC clock that moves only when a test advances it; refuses a naive instant |
| `clock_factory` / `epoch` | The clock class and the fixed reference instant, for non-default starts |
| `id_sequence` / `id_sequence_factory` | Deterministic per-prefix identifiers (`run-0001`, …) |
| `canonicalization_vectors` | The RFC 8785 corpus (ADR-0004), session-scoped and read-only |
| `fixture_file` | Loads a JSON fixture by path relative to `tests/fixtures/` |
| `build_settings` | `ServiceSettings` from an explicit environment mapping, never `os.environ` |
| `workspace_dir` | An isolated per-test directory; the workspace is the isolation boundary |

## Known gap

The `evals/` lane has no test yet. The cavesson L0 policy hook denies every write
whose path contains an `evals` segment, which over-matches this lane and
`actionwitness_core/evals/` in addition to the root-level grading corpus it is
meant to protect. The exemption is recorded and bounded in
`tests/architecture/test_test_lanes.py`, which fails if it ever grows.
