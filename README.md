# ActionWitness

> The agent says it did the thing. **ActionWitness says what actually happened.**

An independent witness for WebMCP-enabled applications: it observes authoritative
business state around an agent's journey, holds that state to an explicit contract,
and turns every silent failure into a portable regression test.

Built for the [WebMCP Challenge](https://webmcp.devpost.com/) (submission Sep 3, 2026, 1:00 PM PDT).

**Status: scaffold.** Structure, boundaries, and tooling are in place; no product
behavior is implemented yet. Source of truth: `actionwitness-functional-spec.md`
(kept beside this repository). Build order and kill-switch dates: `docs/BUILD_ORDER.md`.

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

## Version pinning (spec §29.3) — fill during the §25.1 spike

| Item | Pinned value |
|---|---|
| Chrome build + flag/origin-trial config (`chrome://flags/#enable-webmcp-testing`) | TODO |
| WebMCP API location | `document.modelContext` (verified Aug 2026) |
| Hook package (`use-webmcp-tool` vs `usewebmcp` spike decision) | TODO |
| `webmcp-types` version | TODO |
| `webmcp-evals` package + reporter schema + normalizer version | TODO |
| Primary demo client / fallback | ChatGPT in-app browser / Chrome 149 + DevTools (LD-18) |

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
