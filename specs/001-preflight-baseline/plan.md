# 001 — plan

Order of work (each slice independently green; BUILD_ORDER §13 items 1–2):

1. **Docs alignment first** — sweep v1.8 references, point README at
   `docs/actionwitness-functional-spec.md`, add `docs/adr/` with the ADR
   template. Zero code risk; unblocks everything that cites the spec.
2. **Command surface** — decide and document: `uv run pytest -q` (unit),
   `uv run pytest tests/architecture -q` (arch), ruff format+check, a strict
   `tsc --noEmit` script (constitution: a Vite build alone is not type-check
   coverage), `npm test` (vitest), `npm run build`. Record them in README and
   wire the same names into `pyproject.toml`/`package.json` scripts.
3. **Registry module** — one small module (core-owned, target-neutral) holding
   API error codes + closed state/event enums, exported as both Python enums
   and a generated JSON the frontend can import. Registry precedes handlers so
   names never fork.
4. **Feature flags** — Pydantic settings model in the service: each optional
   integration (buggy_store, evaluator_import, live_evaluator, shopify) is an
   optional block; absence disables only that module (constitution: optional
   integrations fail closed).
5. **Test scaffolding** — the §26-named directories already exist as .gitkeep;
   add conftest fixture builders and one placeholder test per lane so every
   lane is runnable (empty-lane pass, not skips).
6. **WebMCP spike harness** — a minimal page + both candidate hooks behind a
   switch, StrictMode on, registering one read-only `get_workspace_status`
   stub; a manual checklist for the operator's browser run. Results recorded
   in ADR-0002; the pin + lockfile land after the operator decides.
7. **ADR-0001/0003/0004** can be drafted from the spec's locked decisions
   (§32) without a browser; ADR-0004 includes the RFC 8785 vector fixture
   files under `tests/` so 002 consumes them.

Risks / dependencies:

- ADR-0002 is the only external dependency (human + experimental browser);
  everything else proceeds without it. Do not pin the hook or commit the
  frontend lockfile before the spike verdict.
- Keep the registry module in core only if it stays target-neutral; error
  codes referencing HTTP semantics live in the service instead.
