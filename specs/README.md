# specs/ — the ActionWitness implementation runway

Transcribed from `docs/BUILD_ORDER.md` (normative: functional spec v1.9). The
runway is the milestone plan M0–M11 rendered as ordered spec-kit specs. Specs
001 and 002 are authored in full; from 003 onward, the session that closes
spec N authors spec N+1 from this table before starting it (transcribe the
milestone's tasks and exit gate from `docs/BUILD_ORDER.md` §7 — do not invent).

Workflow per spec: `/spec-kit-handoff <id>` → implement citing T-IDs →
`/spec-kit-review` → `/spec-kit-progress`.

## Runway

| Spec | Milestone | Delivers | Exit condition (abbreviated) | Spec §  |
|---|---|---|---|---|
| `001-preflight-baseline` | M0 | ADRs 0001–0004, WebMCP spike, CI commands, test scaffolding, error/enum registry, feature flags | arch tests + `npm ci/test/build` green; one read-only WebMCP tool registers/cleans up without StrictMode duplication | §19, §26, §32 |
| `002-core-kernel` | M1 | Target-neutral core: contracts, ports, evidence/security, engine, journeys, reports | core installs+tests in isolation; RFC 8785 vectors pass; byte-identical reports | §9–10, §16, §18, §22–23 |
| `003-buggy-store-target` | M2 | Standalone Buggy Store + its adapter, `pre_fix`/`post_fix`, discount fault, 5 tool specs | store runs with assurance packages absent; discount fault provably false-success in `pre_fix` only | §13, App. D.2 |
| `004-workspace-persistence` | M3 | Migrations, repositories, locks, cookie authz, rate/resource caps, adapter registry | two clients cannot cross-read/mutate; failures leave no partial state | §15, §17, §20 |
| `005-run-slice` | M4 | Journey A through FastAPI: arm, invoke, observe, evaluate, classify, compare | pre-fix fails `false_success_or_state_mismatch`, post-fix passes; AC-03/04/11/19 + API AC-20 | §6, §12, §16, §22–23 |
| `006-ui-webmcp-confirmation` | M5 | Shared UI panels, WebMCP tools, Journey B confirmation flow — **Tier 1 gate** | Journeys A+B complete in a real browser AND manually without WebMCP; AC-01/06/09/21 | §11, §14, App. D |
| `007-regression-evals` | M6 | Eval case generation, fixture replay, CLI exits 0/1/2, `EvalPanel` | `reproduce_source` recreates exact classification; AC-08/12/15 | §24 |
| `008-evaluator-import` | M7 | Pinned evaluator report import, binding, replay, 2×2 matrix, benchmark artifact — **Tier 2 gate** | full fixture path runs with no Node/LLM/Shopify; AC-16 | §25, §23 |
| `009-release-hardening` | M8 | Docker, single-origin mounting, CI lanes, README, deploy + rollback rehearsal | fresh checkout follows README; tested image is the deployed image; AC-10 | §29, §26, §28 |
| `010-live-model-benchmark` | M9 (Tier 3) | Configured live evaluator run through the M7 import path | AC-17; if red, do not start Shopify | §25, §32 |
| `011-shopify-cart-proof` | M10 (Tier 3) | Exact-origin pairing, theme bridge, `target.cart` observation | AC-18 with no Admin/customer/payment credential | §13.6, §20 |
| `012-webmcp-policy-polish` | M11 (Tier 3) | Retry/confirmation injectors, AbortSignal, declarative form, reconciliation, SSE last | one focused test per shipped feature; AC-02/05/07/13/14 | §11–12, App. D |

## Gates and cut discipline

- **Tier 1 gate** after 006, **Tier 2 gate** after 008 (M7+M6 green). Tier 3
  (010–012) only after both gates; hard cut order is `docs/BUILD_ORDER.md` §12.
- Never cut: target neutrality, false-success detection, safe confirmation,
  matched pre/post comparison, server-derived guidance, deterministic replay,
  exact source-failure fidelity, recorded evaluator import.
- `specs/*/spec.md` is operator-owned once approved; sessions edit `plan.md`
  and `tasks.md` only.

## Runway extension — round-2 features (planned 2026-09-01, shipped 2026-09-02)

Beyond the M0–M11 runway, from `docs/actionwitness-top3-features-round2.md`
(local planning input). All three complete v1.9 requirements that shipped as
honest scaffolds; none is an off-spec invention. All three landed ahead of the
planned schedule: every task is checked in their `tasks.md` files and each has
a shipped exit-gate suite.

| Spec | Feature | Completes | Status |
|---|---|---|---|
| `013-undeclared-change-detection` | the FR-157 diff behind `no_undeclared_changes`, plus the `undeclared_side_effect` injector and third contract template | §9.10, FR-157 | complete — `tests/integration/test_013_exit_gate.py` |
| `014-tool-surface-witness` | live surface capture/watch behind `stable_tool_surface`; `tool_surface_poisoned` demo profile | §9.5, §16.1 | complete — `tests/integration/test_014_exit_gate.py` |
| `015-storefront-witness` | the `external_audit` module, Shopify ten-tool contract pack, merchant-readable report | §12.17, FR-110 | complete server-side — `tests/integration/test_015_exit_gate.py`; the browser client that runs an audit from the workspace is not built (015-T8 note) |
