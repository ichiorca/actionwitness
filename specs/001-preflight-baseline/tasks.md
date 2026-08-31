# 001 — tasks

Cite the T-ID in every commit that advances it.

- [x] T1 — Sweep v1.8 → v1.9 references; README names the spec's real path
      and the full command surface.
- [x] T2 — Add `docs/adr/` with template + index; draft ADR-0001 (adapter
      transport) from spec §32 locked decisions.
- [x] T3 — Draft ADR-0003 (SQLite transaction/lock model) from spec §17/§32.
- [x] T4 — Draft ADR-0004 (RFC 8785 implementation) and commit published +
      repository canonicalization vectors under `tests/` fixtures.
- [x] T5 — Establish Python command surface: ruff format/check config, pytest
      markers, `uv run pytest -q` and arch lane both green.
- [x] T6 — Establish frontend command surface: vitest config, strict
      `tsc --noEmit` script, `npm run build`; document all three.
- [x] T7 — Implement the error-code + closed-enum registry (Python source of
      truth, generated JSON for the frontend) with a unit test that fails on
      undocumented additions.
- [ ] T8 — Implement feature-flag settings model; absent optional config
      disables only its module; unit tests for each absence combination.
- [ ] T9 — Add conftest fixture builders + one runnable placeholder test per
      §26 lane (no skips).
- [ ] T10 — Build the WebMCP spike harness (both candidate hooks switchable,
      StrictMode, one read-only stub tool) + operator browser checklist.
- [ ] T11 — (operator gate) Record spike results in ADR-0002; pin the chosen
      hook + `webmcp-types`; commit the frontend lockfile; record the tested
      browser build.
- [ ] T12 — Verify the exit gate: arch lane, `npm ci`/`test`/`build`, spike
      tool registers/cleans up without StrictMode duplication; confirm no
      unresolved decision touches core public types or persistence semantics.
