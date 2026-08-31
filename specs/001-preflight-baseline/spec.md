# 001 — Preflight baseline (M0)

**Source:** `docs/BUILD_ORDER.md` §7/M0 · functional spec v1.9 §19, §26, §32
**Goal:** make the repository a trustworthy implementation baseline: every
load-bearing decision recorded, every quality command runnable, no unresolved
choice that could change core public types or persistence semantics.

## Acceptance criteria

1. ADR-0001–ADR-0004 exist under `docs/adr/` (context, decision, consequences,
   rejected alternatives, status, date, implementing change) and are accepted:
   - ADR-0001 Buggy Store adapter transport (injected HTTPX; ASGI in tests).
   - ADR-0002 WebMCP lifecycle package — spike `use-webmcp-tool` vs `usewebmcp`
     against the target Chrome/ChatGPT build; pin exactly one (+`webmcp-types`);
     fall back to direct native registration if invocation context is not
     forwarded.
   - ADR-0003 SQLite transaction/lock model (WAL, `BEGIN IMMEDIATE`, FKs,
     5s busy timeout, per-workspace lock lifetime, event sequencing).
   - ADR-0004 RFC 8785 canonicalization implementation (passes published
     vectors + repo fixtures; rejects non-finite numbers).
2. Stale spec v1.8 references replaced with v1.9; README describes the spec at
   its actual path.
3. Formatting, type-check, unit-test, frontend-test, and build commands are
   established and documented (no large framework added solely for linting).
4. Frontend lockfile committed after the ADR-0002 pin; tested browser build
   recorded.
5. Test directories and fixture builders named in spec §26 exist.
6. A machine-readable registry of stable API error codes and closed
   state/event enums exists and is shared by (future) handlers, UI, tests.
7. Feature flags/configuration exist for Buggy Store, evaluator import, live
   evaluator execution, and Shopify; an absent optional config disables only
   that module.

## Exit gate

- `uv run pytest tests/architecture -q` passes.
- `npm ci`, `npm test`, `npm run build` run in the harness frontend (tests may
  initially be only the lifecycle-adapter compatibility cases).
- The selected WebMCP path registers and cleans up one read-only test tool
  without StrictMode duplication.
- No unresolved decision can change core public types or persistence semantics.

## Non-goals

- No production behavior, persistence schema, or business logic (002+).
- No CI service configuration beyond runnable local commands.

## Notes

- Git attach + first commit already landed (`36f15ec`); tasks cover only what
  remains.
- ADR-0002 needs a human at a real browser; sessions prepare the spike harness
  and record results, but the pin itself is an operator decision.
