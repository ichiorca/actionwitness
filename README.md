# ActionWitness

> The agent says it did the thing. **ActionWitness says what actually happened.**

An independent witness for WebMCP-enabled applications: it observes authoritative
business state around an agent's journey, holds that state to an explicit contract,
and turns every silent failure into a portable regression test.

Built for the [WebMCP Challenge](https://webmcp.devpost.com/) (submission Sep 3, 2026, 1:00 PM PDT).

**Live demo:** <https://actionwitness.onrender.com> — the workspace at `/`, the
Buggy Store at [`/demo`](https://actionwitness.onrender.com/demo), health at
[`/healthz`](https://actionwitness.onrender.com/healthz). No credentials, no
login; see [Deployment](#deployment).

**Status.** Tier 1 and Tier 2 are implemented and tested: the target-neutral core
kernel, the standalone Buggy Store and its adapter, workspace persistence, the run
slice, the React/WebMCP workspace with human confirmation, regression evals with a
replay CLI, and external-evaluator report import. 2,000+ deterministic tests,
including the false-success fault proof. Tier 3 modules are present as
configuration-gated scaffolds and ship **off** — see [What is not
here](#what-is-not-here).

**Normative sources.** The functional specification is `docs/actionwitness-functional-spec.md`,
version 1.9; the implementation plan and milestone gates are `docs/BUILD_ORDER.md`.
Both live at those paths in a working tree but are deliberately untracked (see
`.gitignore`) — they are planning inputs held locally by the operator, not published
deliverables. Every in-repo `spec §N` citation refers to v1.9 of that file. The
per-milestone contracts derived from it are tracked under `specs/`.

---

## Contents

- [Why a witness](#why-a-witness)
- [60-second test](#60-second-test)
- [Architecture](#architecture) — full design account in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
- [Where the WebMCP code is](#where-the-webmcp-code-is)
- [Browser setup: Chrome and ChatGPT](#browser-setup-chrome-and-chatgpt)
- [Command surface](#command-surface)
- [Contracts, pre-fix/post-fix, and matched comparison](#contracts-pre-fixpost-fix-and-matched-comparison)
- [Human–agent guidance and confirmation](#humanagent-guidance-and-confirmation)
- [Regression evals: schema, runner, CLI](#regression-evals-schema-runner-cli)
- [Importing an external evaluator report (Tier 2)](#importing-an-external-evaluator-report-tier-2)
- [Auditing a storefront you did not build](#auditing-a-storefront-you-did-not-build)
- [Writing an adapter](#writing-an-adapter)
- [Deployment](#deployment)
- [Security and data handling](#security-and-data-handling)
- [What is not here](#what-is-not-here)
- [Version pinning](#version-pinning)
- [Provenance](#provenance)
- [Attribution](#attribution)
- [License](#license)

---

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

## 60-second test

The point of this run is one screen: a tool call that **returns success** and a
verdict that says the business outcome **failed anyway**.

```bash
git clone <this repository> && cd actionwitness
uv sync                                    # ~20s
uv run pytest tests/integration/test_false_success.py -q
```

That test arms the §10.1 cart contract against the Buggy Store in `pre_fix`, applies
a discount through the recorded tool surface, and asserts three things at once: the
tool reported `status: success`, the independently observed cart total did not move,
and the run classified as `false_success_or_state_mismatch` with execution and
trajectory layers *passing*. Flip the scenario to `post_fix` and the same journey
passes.

To see it in a browser instead, run the two processes and open the workspace:

```bash
uv run buggy-store &                                        # target on :8001
uv run uvicorn actionwitness_service.api.app:create_app --factory --port 8000 &
cd apps/actionwitness_service/frontend && npm ci && npm run dev
```

Then open the printed Vite URL. The workspace runs without WebMCP — every step has
a human control — and reports browser tool support as a fact rather than a
requirement (AC-09).

## Architecture

The diagram and layout below are the orientation. **`docs/ARCHITECTURE.md` is the
full account** — why the layers are shaped this way, how each invariant is
mechanically enforced rather than documented, where the WebMCP surfaces are, the
evidence and consent models, and a candid list of the system's known limits.

```mermaid
flowchart TB
    subgraph browser["Browser (one origin)"]
        UI["React workspace<br/>/"]
        SF["Buggy Store storefront<br/>/demo"]
        MC["navigator.modelContext<br/>WebMCP tools"]
    end

    subgraph service["actionwitness_service (process 1)"]
        API["FastAPI /api/v1"]
        ENG["orchestration · guidance<br/>confirmation · evidence"]
        DB[("SQLite<br/>workspaces · runs · evidence")]
    end

    subgraph target["buggy_store (process 2)"]
        SAPI["/demo/api/v1"]
        SDB[("SQLite<br/>carts · orders")]
    end

    CORE["actionwitness_core<br/>contracts · engine · evals · reports<br/>(target-neutral, no app imports)"]
    ADPT["integrations.buggy_store<br/>ManagedTargetAdapter + ObservationProvider"]

    UI --> MC
    MC -->|recorded invocation| API
    UI --> API
    SF --> SAPI
    API --> ENG
    ENG --> CORE
    ENG --> ADPT
    ADPT -->|"tool call (self-report)"| SAPI
    ADPT -->|"independent observation"| SAPI
    ENG --> DB
    SAPI --> SDB
```

The two arrows from the adapter to `/demo/api/v1` are the product. One carries the
tool call whose result is *evidence*; the other is a separate authoritative read
whose result is *proof*. They are never the same response — a successful tool
response is never persisted as observed state (constitution §4).

### Repository layout (spec §18)

    packages/actionwitness_core      target-neutral assurance library (no app/demo/vendor imports — enforced)
    apps/actionwitness_service       FastAPI app, orchestration, guidance, persistence, CLI, React frontend
    integrations/buggy_store         core ports <-> Buggy Store versioned HTTP API + WebMCP bridge
    integrations/google_evals        pinned webmcp-evals report adapter (import/normalize/correlate)
    integrations/shopify             external-surface audit (audit.py, pack.py — live, used by /api/v1/audits); Tier 3 dev-store target (adapter.py, observation.py — scaffold, router unmounted)
    examples/buggy_store             independently runnable demo target (no assurance-stack imports — enforced)
    shopify_bridge                   Tier 3 theme bridge (placeholder)
    tests/                           architecture, unit, integration, adapters, contracts, evals lanes

Dependency direction is enforced, not documented. `actionwitness_core` imports no
application, integration, demo, evaluator-vendor, or commerce module, and the demo
target imports nothing from the assurance stack:

```bash
uv run pytest tests/architecture/test_import_boundaries.py -q   # forbidden-import gate
uv run python scripts/core_only_isolation.py                    # core installs and tests ALONE
uv run python scripts/store_only_isolation.py                   # store installs and runs ALONE
```

The last two build a fresh virtualenv and install exactly one distribution into it.
They are the only proof that "installs with every other package absent" is true
rather than intended.

## Where the WebMCP code is

§29.2 asks for this explicitly, because "we use WebMCP" is not a location.

| Style | File | What it does |
|---|---|---|
| **Native** (`navigator.modelContext.registerTool`) | `apps/actionwitness_service/frontend/src/webmcp/adapter.ts` | The **only** file that touches the WebMCP API. Owns registration, StrictMode-safe cleanup, error normalization, and the cancellation-sensitive direct path (ADR-0002 rule 3). |
| **Hook-based** (`use-webmcp-tool@0.2.0`) | `apps/actionwitness_service/frontend/src/spike/hookPath.tsx` | The pinned lifecycle package, exercised in the ADR-0002 spike. Not used for cancellation-sensitive tools — no path in the tested build forwards the per-invocation signal. |
| **Declarative** | not used | The MVP registers imperatively. Declarative form annotation is roadmap scope; recorded here so its absence is a statement rather than an omission. |
| Harness tool definitions | `apps/actionwitness_service/frontend/src/tools/harnessTools.ts` | `list_contract_templates`, `get_outcome_contract`, `arm_outcome_contract`, `verify_outcome`, `get_run_findings`, `reset_workspace`, `create_regression_eval`, `run_regression_eval` |
| Target tool definitions | `apps/actionwitness_service/frontend/src/integrations/buggyStore/tools.ts` | `search_catalog`, `get_cart`, `update_cart`, `apply_discount`, `proceed_to_checkout` |

The tool set changes with workspace state (spec §11.5), so which tools are callable
is a function of the phase — see [guidance](#humanagent-guidance-and-confirmation).

**No Python-side WebMCP registration exists, by rule.** Browser registration lives
in the TypeScript adapter; the Python side records invocations that arrive through
`/api/v1`.

## Browser setup: Chrome and ChatGPT

WebMCP is behind a flag in the tested build.

1. Chrome 151 stable. Open `chrome://flags/#enable-webmcp-testing`, set it to
   **Enabled**, and relaunch.
2. Open the workspace. The capability bar reports whether
   `navigator.modelContext` / `document.modelContext` was found. Both were present
   in the tested build (verified live 2026-08-31).
3. Drive the tools from the ChatGPT in-app browser, or from Chrome DevTools:

   ```js
   await navigator.modelContext.getTools();
   await navigator.modelContext.executeTool("get_run_findings", { limit: 3 });
   ```

If WebMCP is absent the workspace still works end to end — that is AC-09, and it is
tested (`panels.test.tsx`, "reports a browser without WebMCP as a fact, not a
failure"). Nothing in the human path requires a browser agent.

## Command surface

These are the only commands the project supports; CI runs exactly these names
(`.github/workflows/ci.yml`, spec §26). No linting framework beyond `ruff` is used
on the Python side.

### Python — from the repository root

| Command | Purpose |
|---|---|
| `uv sync` | Resolve and install the `uv` workspace (all members + dev group) |
| `uv run pytest -q` | Full Python suite — every lane under `tests/` |
| `uv run pytest tests/architecture -q` | Architecture gates (forbidden imports, layering, isolation, release hygiene) — spec §26.7 |
| `uv run python scripts/core_only_isolation.py` | Install ONLY `actionwitness_core` in a clean venv and run its suite there (AC-19) |
| `uv run python scripts/store_only_isolation.py` | Install ONLY the Buggy Store in a clean venv, run a real storefront journey, and run its suite there (AC-19) |
| `uv run python scripts/scan_for_secrets.py` | Secret-shape scan over tracked files (CI gate) |
| `uv run ruff format --check .` | Formatting gate (add `--diff` to see it; drop `--check` to apply) |
| `uv run ruff check .` | Lint gate (`--fix` to apply safe fixes) |
| `uv run buggy-store` | Run the demo target alone on `:8001`, with no assurance package involved |

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
| `npm run lint` | ESLint (type-checked rules, hooks, jsx-a11y) |
| `npm test` | Vitest (jsdom) — adapter lifecycle, polling, panels, confirmation |
| `npm run build` | Vite production bundle |
| `npm run typecheck:e2e` | Strict `tsc --noEmit` over the browser lane's own sources |
| `npm run test:e2e` | **Tier 3, opt-in.** Playwright against the composed deployment — see below |

#### The automated browser lane (Tier 3, never release-gating)

`npm run test:e2e` builds the one-origin tree spec §29.1 describes, starts both
application processes, and drives the harness UI, the storefront, and the WebMCP
tool surface in a real browser. It covers the seam nothing else reaches: the
Python suite never opens a browser, and the jsdom suite stubs `fetch` and owns
its own `document.modelContext`.

Spec §26 makes this tier **conditional** — "their absence shall never fail the
release-gating suite" — so it is outside `uv run pytest -q`, outside `npm test`,
outside `npm run typecheck`, and outside every CI job. `tests/browser/` remains
the manual §26.4 checklist it was, and this lane does not replace it: Chromium
ships no WebMCP without the flag ADR-0002 pins, so the registry is substituted
while everything above and below it is production code.

Requires `npx playwright install chromium` once. Working files land in
`apps/actionwitness_service/frontend/.e2e/` and are git-ignored.
`apps/actionwitness_service/frontend/e2e/README.md` documents what each spec
holds and why the lane respects FR-009's rate limits rather than relaxing them.

### Storefront frontend — from `examples/buggy_store/frontend`

The demo target's own human UI. Built and tested independently of the harness
frontend (spec §29.1), and deliberately free of WebMCP: this is the storefront a
person uses when no agent, no harness, and no browser-tool support is present
(AC-09).

| Command | Purpose |
|---|---|
| `npm ci` | Install from the committed lockfile |
| `npm run typecheck` | **Strict `tsc --noEmit`.** Separate gate from the build |
| `npm run lint` | ESLint, mirroring the harness rules |
| `npm test` | Vitest (jsdom, no `document.modelContext` by design) |
| `npm run build` | Vite production bundle |
| `npm run dev` | Serve the storefront, proxying `/demo` to a local store on port 8001 |

Both lockfiles are committed; `npm ci` is the reproducible install path for each
frontend. The harness tree records the ADR-0002 pin (`use-webmcp-tool@0.2.0`).

## Contracts, pre-fix/post-fix, and matched comparison

An **outcome contract** states what must be true of authoritative business state
after a journey — preconditions, expected tools, and assertions with `critical` or
`warning` severity (spec §9.4, §10.1). Built-in templates are seeded at startup by
the integration that owns them; a workspace selects one and arms a run against it.

**`pre_fix` / `post_fix` are demo profiles, not a deployed patch.** The Buggy Store
ships with injectable failure profiles (spec §13.3). `pre_fix` activates one —
`apply_discount` reports success and persists nothing. `post_fix` runs the same code
path with the fault inactive. Switching between them changes *the demo target's
configuration*, and nothing about ActionWitness is patched, rebuilt, or reconfigured
in between. Anyone reading a `pre_fix` failure as "a bug we fixed" has it backwards:
the fault is deliberate, permanent, and the point.

**Matched comparison** (spec §12): two runs are comparable only when their contract,
target, scenario, and fixture agree. A rerun that differs in any of those stays a
valid run and reports `not_comparable`, naming the differing fields, rather than
producing a misleading delta.

## Human–agent guidance and confirmation

Every nonterminal workspace state produces exactly one `GuidanceState` naming one
actor and one next action (FR-120). The phases are
`no_contract`, `proposing`, `candidates`, `contract_ready`, `armed`, `running`,
`awaiting_confirmation`, `verifying`, `passed`, `passed_with_warnings`, `failed`,
`eval_ready`, `eval_running` (spec §11.5). The banner, the controls, the tool
`next_action`, and the action history all name the same action code at every
transition — asserted end to end in `tests/integration/test_006_exit_gate.py`.

**Protected actions require a server-issued human confirmation** bound to the
workspace, run, action, arguments, and expiry. An agent cannot create, broaden, or
approve its own consent. The dialog preselects neither choice, traps and restores
focus, and is rebuildable after a page refresh. Denial, expiry, and cancellation
each create no order — and one approval produces exactly one order, tested in
`tests/integration/test_journey_b.py`.

**Cancellation and recovery.** In-flight work is cancellable; obsolete polling
responses are ignored rather than applied; a partially completed operation stays
visible instead of being silently retried. Every one of those has a test.

**Manual equivalent.** Every agent-operable step has a human control that reaches
the same endpoint. The journey needs no browser agent
(`test_gate_2_the_whole_journey_needs_no_browser_agent`).

## Regression evals: schema, runner, CLI

A failed run can be turned into a portable regression case: fixture, trajectory,
and outcome expectations, redacted and content-hashed (spec §24).

- **Published schema:** `packages/actionwitness_core/src/actionwitness_core/evals/regression_eval_case_1_0.json`
- **Runner:** `actionwitness_core.evals` (pure, synchronous, deterministic) driven by
  `actionwitness_service.application.eval_runner`
- **A sample case** is produced by
  `uv run pytest tests/integration/test_eval_case_generation.py -q`, or by
  `create_regression_eval` from a failed run in the UI, and downloaded from
  `GET /api/v1/evals/{case_id}/case.json`.

```bash
uv run actionwitness eval validate path/to/case.json
uv run actionwitness eval run path/to/case.json --environment current
uv run actionwitness eval run path/to/case.json --environment reproduce_source
```

Exit codes are fixed by FR-088 and are the CI contract:

| Code | Meaning |
|---|---|
| `0` | Replay matched the case's outcome expectations |
| `1` | Replay ran and **differed** — an unrelated or additional critical classification |
| `2` | The case is invalid, or the harness could not execute it. Never `1`: nothing was replayed, so there is no expectation to differ from |

`--environment reproduce_source` replays against the implementation the case was
generated from and must reproduce the original failure and the exact critical set.
`current` replays against today's code and is the regression gate.

## Importing an external evaluator report (Tier 2)

ActionWitness correlates the pinned Google `webmcp-evals` reporter's output with its
own outcome layer, producing a dual-layer benchmark (spec §9.9, §25.3). It never
re-implements that evaluator.

```
POST /api/v1/benchmarks/{benchmark_id}/imports
```

- **Pinned evaluator:** `webmcp-evals@0.0.4` (`fe33c1b`) — see ADR-0005
- **Checked-in redacted fixture:** under `tests/fixtures/`, used by the import and
  correlation tests
- **Binding rules:** binding is **explicit** and fails closed on weak addressing. A
  trial is bound to a run only by an unambiguous identifier; a report that cannot be
  bound is reported as unbound rather than guessed at. This matters because the
  upstream reporter does not yet emit a stable trial ID — the correlation would
  otherwise be a heuristic presented as a fact.
- **Untrusted by construction:** an imported report is validated for size and
  allowlisted schema before parsing, all displayed text is escaped, executable or
  HTML content is rejected, and replay is limited to allowlisted target tools.

_Upstream stable-trial-ID reporter issue: **drafted, not filed** — the text is
kept with the decision records outside this repository (see below), verified
against `webmcp-evals` v0.0.4 (`fe33c1b`). Filing is an outward-facing act on another
project's repository and is the operator's call (spec §25.3, ADR-0005)._

## Auditing a storefront you did not build

Some storefronts have agent tools on them that their owners never installed and
cannot test. Storefront Witness audits one such surface — a single origin the
operator asserts they are authorized on — and returns a report written for the shop
owner: which agent tools work, which report success while the store does not change,
which were deliberately left alone, and what to fix first.

```
GET  /api/v1/audits/packs             # the built-in contract packs, offered (FR-161)
POST /api/v1/audits                   # assert one authorized origin
GET  /api/v1/audits/current           # the live audit, or an explicit null
POST /api/v1/audits/current/evidence  # the pass: browser transcript in, sealed report out
GET  /api/v1/audits/current/report    # the sealed report, re-verified before it is served
POST /api/v1/audits/current/cancel    # release the workspace's one live-audit slot
```

An audit now **finishes**. The submission classifies the browser's transcript
(`pack_id`, the `getTools()` enumeration, what each exercised tool claimed, and the
raw cart reads before and after), composes the merchant report, writes it as a
content-addressed artifact (ADR-0007), and moves the audit to §22's terminal `completed` in the
same transaction that records the artifact row — so the next audit is no longer
blocked behind the 24-hour workspace sweep. `cancel` is the other exit, for an
audit begun against the wrong origin.

**The browser client for this flow is not built.** The server path is complete,
tested end to end over HTTP (`tests/integration/test_external_audit_pass.py`), and
entirely API-driven — but there is no audit UI in the React workspace, and no
frontend module references `/audits`. Today this is exercised by an API client
that supplies the transcript, not from the app. What is missing is the browser
half that runs `getTools()`, exercises the pack, and reads `cart.js` in the
operator's own session; nothing in the endpoints above will do that for you.

- **Off unless configured.** `external_audit` ships disabled, so an anonymous
  workspace can never assert authorization for an origin the deployment did not
  allowlist (`EXTERNAL_AUDIT_ALLOWED_ORIGINS`, server-controlled).
- **The packs are offered, never chosen for you.** `match_pack` returns every pack
  a surface satisfies and picks none, and the submission must name one — choosing
  on the operator's behalf would decide, against a storefront somebody depends on,
  whether a write path gets exercised.
- **A submission is size-capped before it is parsed** (FR-117, 256 KiB). The cart
  payload is the one part of the request the audited storefront controls rather
  than the operator, so the bound precedes the JSON parser rather than sitting
  behind it as a validator.
- **An unread channel is never a pass.** Absent observations arrive as `null` and
  every exercised tool is classified `unobserved` — §12.17's
  `observation_unavailable`. A *malformed* read is refused as a 422 instead, because
  a broken submission and an unobservable storefront are different facts.
- **A tampered report is refused, not served.** The stored document is re-verified
  against its recorded hash on every read, and the refusal names neither the path
  nor the hash — together they are what a forger would need.
- **One origin, never a list.** The request model has no field that accepts a
  collection. There is no crawler and no discovery path.
- **The harness makes no request to the audited site.** The independent cart read
  happens in the operator's own browser, through the platform's session API. No
  module in the audit imports an HTTP client, which is asserted in the architecture
  lane rather than promised in a comment.
- **Never checkout, never an order.** `proceed_to_checkout` and `manage_orders` are
  reported as present and reachable, and no shipped contract pack can dispatch them.
- **A pass is evidence, not a warranty.** The report states its own limits.

The context this feature was built for — and the specific claims this project does
and does not make about it — is `docs/storefront-witness.md`. The public findings
cited there are **published third-party reports**, attributed and dated; ActionWitness
has scanned no brand it does not own.

## Writing an adapter

Three protocols in `packages/actionwitness_core/src/actionwitness_core/ports/__init__.py`:

| Protocol | Responsibility |
|---|---|
| `TargetAdapter` | Publishes a tool surface and executes an invocation |
| `ManagedTargetAdapter` | …plus `prepare`: restore a fixture and select a scenario. For targets the harness controls |
| `ExternalTargetAdapter` | For targets it does not. Cannot restore fixtures; forbidden operations refuse explicitly |
| `ObservationProvider` | Reads authoritative state through a channel independent of tool responses |

A **minimal non-commerce example** lives in
`tests/adapters/test_non_commerce_adapter.py` — a fake target with no cart, no
money, and no product, evaluated end to end through the same core interfaces. It
exists to prove the core is target-neutral rather than commerce software with the
labels changed.

**Supported external target scope.** Exactly one: an authorized Shopify development
store, one configured variant and currency, cart-only, and only when the operator
supplies the configuration. It is **not enabled in any build** — configuring it
reports `disabled` with a reason naming the missing adapter rather than switching
anything on. Every other external target is roadmap scope, not V1. The audit
surface above is a different thing and does not need it: it observes a storefront
through the operator's browser and drives no target adapter at all.

## Deployment

One Render web service, one Docker image, one origin (spec §29.1, ADR-0006).

```bash
docker build -t actionwitness .
docker run --rm -p 8000:8000 -e HARNESS_PUBLIC_ORIGIN=http://localhost:8000 actionwitness
```

- `/` — harness workspace `/api/v1` — harness API
- `/demo` — Buggy Store storefront `/demo/api/v1` — Buggy Store API
- `/healthz` — liveness, plus `public_origin`, `assets_mounted`, `schema_version`,
  `database` (`ok` / `unavailable`) and `origin_policy` (`configured` /
  `unconfigured`)

The image runs **two processes in two virtualenvs**: the store never shares an
environment with the assurance stack, and the only route between them is the
versioned HTTP API on loopback. `--workers 1` is load-bearing — ADR-0003's SQLite
lock model assumes a single writer, so a second worker is a correctness change.
ADR-0006 records the whole composition and what it costs.

`HARNESS_PUBLIC_ORIGIN` must be the **exact deployed origin**. It drives both the
cookie's `Secure` attribute and the origin allowlist; set it wrong and the service
comes up healthy while refusing every mutation. `/healthz` reports the value it
resolved, which is how you find that mistake in seconds instead of an hour.

That check is not merely descriptive. `/healthz` answers **503** with
`"status": "degraded"` when the database cannot be read, or when a *production*
deployment has no valid `HARNESS_PUBLIC_ORIGIN` — in which case `OriginPolicy`
falls back to comparing each request against its own origin, which is the right
default for documented local development and an allowlist of whatever the caller
claims anywhere else. The database probe reads a real table on every call rather
than reporting a value captured at startup, because SQLite happily creates an
empty database for a path that no longer exists and a `SELECT 1` would answer
just as cheerfully from the file it had silently made. A red health check holds
the new deploy back and leaves the previous one serving, which is why this is
reported rather than made a crash on boot.

Environment variables are documented in
`apps/actionwitness_service/src/actionwitness_service/config.py` and `.env.example`.

## Security and data handling

**Threat boundary.** Everything crossing into the service is untrusted: HTTP
bodies, WebMCP arguments and results, imported evaluator reports, URLs, and adapter
responses. Python boundaries are explicit Pydantic models that forbid unknown
fields; TypeScript receives external values as `unknown` and narrows them at
runtime.

- **No credential is needed to run the demo.** The Buggy Store path uses none.
  Model-provider credentials belong to the pinned evaluator's own process
  environment (FR-099) and are never in the Docker image, the frontend bundle, the
  health response, logs, evidence, or fixtures. Configuration records the *name* of
  a credential variable, never its value.
- **Anonymous workspaces.** A cryptographically random cookie, `HttpOnly` and
  `SameSite=Strict` always, `Secure` outside documented local HTTP development. It
  is an isolation boundary; a `workspace_id` from a tool argument is never an
  authorization mechanism.
- **Origin validation** on every mutating request, compared for equality after
  normalization — no prefix or suffix matching.
- **`Permissions-Policy: tools=(self)`** and `Origin-Agent-Cluster: ?1` (spec §20.1).
  No CORS is offered to any origin; the frontend and API are same-origin by design.
- **A strict `Content-Security-Policy`** — `default-src 'none'` with `'self'` for
  scripts, fetches, styles and fonts, and no `'unsafe-inline'` or `'unsafe-eval'`.
  It is safe to be that strict because the bundle shape it assumes is asserted:
  `tests/architecture/test_bundle_shape.py` fails if either frontend gains an
  inline script, an inline style, CSS-in-JS, `eval`, or an off-origin asset.
- **Rate limits** keyed on the direct peer, or on trusted-proxy metadata only when
  the peer is an operator-configured proxy. An arbitrary client-supplied forwarding
  header is ignored.
- **Structured logs carry identifiers, status, duration, and classification —
  never payloads.** The log model has a closed field set, so there is no `extra`
  dict to slip a value into, and an unmatched request path is reduced to a sentinel
  rather than logged raw.
- **Evidence is append-only, canonically serialized (RFC 8785), and hash-linked.**
  Verification failure produces an explicit non-pass; it never degrades to success.
  Artifact files are named after the digest of their own document and written
  atomically, so a committed row can never come to describe bytes that were
  overwritten or half-written — ADR-0007 records the two defects that made that
  necessary, and there is one verification implementation rather than one per
  reader.
- **Data retention.** Demo data is ephemeral. Stale workspaces are swept at startup
  and hourly. Nothing personal is requested or required.

Report a security issue by opening an issue without exploit detail, and we will
arrange a private channel.

## What is not here

Named, not hidden. Each of these is switched off in this deployment and reports its
own state at `GET /api/v1/workspace` under `modules`.

| Module | State | Why |
|---|---|---|
| `shopify` | **off** (Tier 3, optional) | The adapter, the theme bridge, and `/api/v1/shopify` are unmounted scaffolds, so the module reports `disabled` **even when all four environment variables are set** — the reason names the build rather than blaming the operator, and no settings object is exposed, because a populated one is how "is it configured?" quietly becomes a stand-in for "is it on?". Switching it on is one edit (`_SHOPIFY_ADAPTER_SHIPPED`) once the adapter and route land. |
| `live_evaluator` | **off** (Tier 3, optional) | Live model runs need a provider credential. Recorded fixtures are used instead, and are labelled `recorded_fixture` rather than `live_model_run`. |
| `external_audit` | **off by default** (Tier 3, optional) | Implemented and tested; ships disabled because an anonymous workspace must never assert authorization for an origin the deployment did not configure. Enabling it needs `EXTERNAL_AUDIT_ALLOWED_ORIGINS` — see [above](#auditing-a-storefront-you-did-not-build). |

Other known limitations:

- **SQLite, single worker, single instance.** Correct for the MVP and stated as a
  constraint rather than a default. Horizontal scaling needs a different store.
- **Ephemeral demo data.** A redeploy restarts from a seeded state.
- **Declarative WebMCP annotation is unused.** Registration is imperative.
- **The `/demo` proxy buffers**, capping a storefront request body at 64 KiB.
- **Screenshots and demo video are not yet in this README.** They are captured
  against the deployed URL.
- **The store process is not supervised.** If it exits, the harness stays up and
  `/demo/api/v1` answers a named `TARGET_UNAVAILABLE` until the container restarts.

## Version pinning

Filled from the §25.1 spike run of 2026-08-31; full readings and the decision
rule are in ADR-0002. Re-run the spike
(`npm run dev`, open **`/spike.html`**) before changing any row.

| Item | Pinned value |
|---|---|
| Chrome build + flag/origin-trial config (`chrome://flags/#enable-webmcp-testing`) | Chrome 151.0.0.0 stable (Windows), flag **Enabled** |
| WebMCP API location | `document.modelContext` **and** `navigator.modelContext` (verified live 2026-08-31; `registerTool`/`getTools`/`executeTool`/`ontoolchange`) |
| `getTools()` / `toolchange` | both present; `toolchange` fires per change (bursts not coalesced, none dropped); descriptors carry descriptions + `readOnlyHint`/`untrustedContentHint` → `stable_tool_surface` viable |
| Hook package (`use-webmcp-tool` vs `usewebmcp` spike decision) | `use-webmcp-tool@0.2.0` (exact); cancellation-sensitive tools use direct native registration — no path in this build forwards the per-invocation signal |
| `webmcp-types` version | `0.1.5` (exact) |
| `webmcp-evals` package + reporter schema + normalizer version | `webmcp-evals@0.0.4` (`fe33c1b`); explicit binding, fail-closed on weak addressing (ADR-0005) |
| Primary demo client / fallback | ChatGPT in-app browser / Chrome 151 + `#enable-webmcp-testing` + DevTools (LD-18) |

Decisions are recorded as numbered ADRs. **The records themselves are kept
outside this repository at the operator's direction**, so the `ADR-000N`
references here and throughout the source name a decision rather than a file you
can open from a clone.

## Provenance

All WebMCP-facing work in this repository — the browser adapter, the tool surfaces,
the harness, the demo target, the evals, and the service — was written during the
challenge's eligible implementation period (from 2026-08-27, the date the project
took its current name). There are no pre-existing assets carried in beyond the
open-source dependencies listed below.

## Naming

"ActionWitness" is the project name as of Aug 27, 2026. PyPI distributions are
`actionwitness-core`, `actionwitness-service`, and `actionwitness-integration-*`;
the CLI is `actionwitness`. Unrelated to the similarly-named `mcpact` project
(contract testing for MCP *servers*), which occupies a different layer.

## Attribution

ActionWitness is Apache-2.0. It builds on, and requires the notices of:

| Component | License | Role |
|---|---|---|
| [FastAPI](https://github.com/fastapi/fastapi), [Starlette](https://github.com/encode/starlette), [Uvicorn](https://github.com/encode/uvicorn) | MIT / BSD-3-Clause | Service boundary and ASGI server |
| [Pydantic](https://github.com/pydantic/pydantic) | MIT | Boundary validation |
| [HTTPX](https://github.com/encode/httpx) | BSD-3-Clause | Adapter and proxy transport |
| [aiosqlite](https://github.com/omnilib/aiosqlite) | MIT | Async SQLite driver |
| [React](https://github.com/facebook/react) | MIT | Workspace UI and storefront |
| [Vite](https://github.com/vitejs/vite), [Vitest](https://github.com/vitest-dev/vitest) | MIT | Frontend build and tests |
| [TypeScript](https://github.com/microsoft/TypeScript), [ESLint](https://github.com/eslint/eslint), [typescript-eslint](https://github.com/typescript-eslint/typescript-eslint) | Apache-2.0 / MIT | Frontend type-check and lint |
| [`use-webmcp-tool`](https://www.npmjs.com/package/use-webmcp-tool), [`webmcp-types`](https://www.npmjs.com/package/webmcp-types) | MIT | WebMCP lifecycle hook and types (ADR-0002) |
| [`webmcp-evals`](https://github.com/webmachinelearning/webmcp) (Google) | Apache-2.0 | The pinned call-level evaluator whose reports are imported — complemented, never reimplemented (ADR-0005) |
| [pytest](https://github.com/pytest-dev/pytest), [ruff](https://github.com/astral-sh/ruff), [uv](https://github.com/astral-sh/uv) | MIT / Apache-2.0 | Python test, lint, and packaging toolchain |

RFC 8785 (JSON Canonicalization Scheme) is implemented from the specification; see
ADR-0004.

## License

Apache-2.0 — see `LICENSE`.
