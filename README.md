# ActionWitness

> The agent says it did the thing. **ActionWitness says what actually happened.**

An independent witness for WebMCP-enabled applications: it observes authoritative
business state around an agent's journey, holds that state to an explicit contract,
and turns every silent failure into a portable regression test.

Built for the [WebMCP Challenge](https://webmcp.devpost.com/) (submission Sep 3, 2026, 1:00 PM PDT).

**Status: scaffold.** Structure, boundaries, and tooling are in place; no product
behavior is implemented yet.

**Normative sources.** The functional specification is `docs/actionwitness-functional-spec.md`,
version 1.9; the implementation plan and milestone gates are `docs/BUILD_ORDER.md`.
Both live at those paths in a working tree but are deliberately untracked (see
`.gitignore`) — they are planning inputs held locally by the operator, not published
deliverables. Every in-repo `spec §N` citation refers to v1.9 of that file. The
per-milestone contracts derived from it are tracked under `specs/`.

## Why a witness

A call-level evaluator asks *did the model call your tools correctly?* ActionWitness
asks the question that survives a green test suite: *did those calls leave the
business in the state the user asked for?* It watches through a channel the tool
does not control — canonical server state, or a platform's own session API — so a
tool that reports success while changing nothing has nowhere to hide.

> A call-level evaluator tests whether the model calls your tools correctly;
> ActionWitness tests whether your tools did what they claimed. **Run both.**

It imports and correlates results from the pinned Google `webmcp-evals` reporter
into a dual-layer benchmark (spec §9.9). It never reimplements that evaluator and
is never a replacement for it.

Two distinctions worth stating up front (spec §2.2):

- `webmcp-evals` **smoke mode** executes authored calls against a live page and
  passes on execution success. The ActionWitness replay runner executes a
  *failure-derived* case against a *restored fixture* in an isolated workspace and
  passes on *business-outcome expectations* with classification fidelity.
- A `result` matcher reads the same channel that can lie. ActionWitness observation
  providers (canonical target state, Shopify same-session Cart API) are independent
  of tool-return text by construction.

## Repository layout (spec §18)

    packages/actionwitness_core      target-neutral assurance library (no app/demo/vendor imports — enforced)
    apps/actionwitness_service       FastAPI app, orchestration, guidance, persistence, CLI, React frontend
    integrations/buggy_store         core ports <-> Buggy Store versioned HTTP API + WebMCP bridge
    integrations/google_evals        pinned webmcp-evals report adapter (import/normalize/correlate)
    integrations/shopify             Tier 3 authorized development-store adapter
    examples/buggy_store             independently runnable demo target (no assurance-stack imports — enforced)
    shopify_bridge                   Tier 3 theme bridge (placeholder)
    tests/                           architecture gates live now; the rest fills per tier

## Quick start (scaffold level)

    uv sync                                  # resolve the workspace
    uv run pytest tests/architecture -q      # architecture gates — green from day one
    uv run actionwitness eval validate x     # CLI surface (exits 2 until Tier 2 lands)
    cd apps/actionwitness_service/frontend && npm install && npm run dev

60-second judge test: **TODO before submission** (spec §29.2).

## Command surface

These are the only commands the project supports; CI runs exactly these names
(BUILD_ORDER §7/M0, spec §26). No linting framework beyond `ruff` is used.

### Python — from the repository root

| Command | Purpose |
|---|---|
| `uv sync` | Resolve and install the `uv` workspace (all members + dev group) |
| `uv run pytest -q` | Full Python suite — every lane under `tests/` |
| `uv run pytest tests/architecture -q` | Architecture gates only (forbidden imports, layering, core-only install) — spec §26.7 |
| `uv run python scripts/core_only_isolation.py` | Install ONLY `actionwitness_core` in a clean venv and run its suite there (AC-19); also run by the architecture lane |
| `uv run python scripts/store_only_isolation.py` | Install ONLY the Buggy Store in a clean venv, run a real storefront journey, and run its suite there (AC-19); also run by the architecture lane |
| `uv run ruff format --check .` | Formatting gate (add `--diff` to see it; drop `--check` to apply) |
| `uv run ruff check .` | Lint gate (`--fix` to apply safe fixes) |

One generator is part of the surface:

| Command | Purpose |
|---|---|
| `uv run python -m actionwitness_service.api.registry_export` | Regenerate the shared name registry the frontend imports |

The registry (`apps/actionwitness_service/frontend/src/generated/registry.json`)
is the single source of stable API error codes and closed state/event enums, so
handlers, UI, and tests cannot fork names. It is committed; `uv run pytest -q`
fails if it drifts from its Python source.

`pytest` markers select a lane inside the full run, e.g. `uv run pytest -q -m architecture`.
Registered markers: `architecture`, `unit`, `integration`, `adapters`, `contracts`,
`evals`, `benchmarks`, `guidance`, `shopify`, `browser`.

### Frontend — from `apps/actionwitness_service/frontend`

| Command | Purpose |
|---|---|
| `npm ci` | Install from the committed lockfile |
| `npm run typecheck` | **Strict `tsc --noEmit`.** A Vite build is bundling only and is *not* type-check coverage |
| `npm test` | Vitest (jsdom) — lifecycle-adapter compatibility cases |
| `npm run build` | Vite production bundle |

> **`npm ci` is not runnable yet.** The frontend lockfile is committed only after
> the ADR-0002 WebMCP hook spike pins exactly one package (`docs/adr/0002-webmcp-lifecycle-package.md`).
> Until then use `npm install` locally and expect the pin to change the tree.

`examples/buggy_store/frontend` builds independently and gains the same four
commands when its Tier 1 UI lands (spec §29.1).

## Version pinning (spec §29.3)

Filled from the §25.1 spike run of 2026-08-31; full readings and the decision
rule are in `docs/adr/0002-webmcp-lifecycle-package.md`. Re-run the spike
(`npm run dev`, open **`/spike.html`**) before changing any row.

| Item | Pinned value |
|---|---|
| Chrome build + flag/origin-trial config (`chrome://flags/#enable-webmcp-testing`) | Chrome 151.0.0.0 stable (Windows), flag **Enabled** |
| WebMCP API location | `document.modelContext` **and** `navigator.modelContext` (verified live 2026-08-31; `registerTool`/`getTools`/`executeTool`/`ontoolchange`) |
| `getTools()` / `toolchange` | both present; `toolchange` fires per change (bursts not coalesced, none dropped); descriptors carry descriptions + `readOnlyHint`/`untrustedContentHint` → `stable_tool_surface` viable |
| Hook package (`use-webmcp-tool` vs `usewebmcp` spike decision) | `use-webmcp-tool@0.2.0` (exact); cancellation-sensitive tools use direct native registration — no path in this build forwards the per-invocation signal |
| `webmcp-types` version | `0.1.5` (exact) |
| `webmcp-evals` package + reporter schema + normalizer version | TODO — pinned in M7 (ADR-0005) |
| Primary demo client / fallback | ChatGPT in-app browser / Chrome 151 + `#enable-webmcp-testing` + DevTools (LD-18) |

## Naming

"ActionWitness" is the project name as of Aug 27, 2026. PyPI distributions are
`actionwitness-core`, `actionwitness-service`, and `actionwitness-integration-*`;
the CLI is `actionwitness`. Unrelated to the similarly-named `mcpact` project
(contract testing for MCP *servers*), which occupies a different layer.

## Required README sections still to write (spec §29.2)

Architecture diagram - module/dependency map & core-only install - WebMCP code
locations (native/hook/declarative) - Chrome + ChatGPT test instructions -
screenshots/GIF - security & data-handling notes - known limitations -
attribution/notices - eval schema + CLI usage + sample case - adapter protocol
docs - pre/post-fix semantics - guidance states - Tier 2 import command +
fixture + binding rules - upstream stable-trial-ID issue link (once filed, §25.3)
- pre-existing vs eligible-period work distinction.

## License

Apache-2.0 — see `LICENSE`.
