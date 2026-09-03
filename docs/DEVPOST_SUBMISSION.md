# Devpost submission draft

## Project title

ActionWitness

## Tagline

Your agent says “done.” ActionWitness checks the truth—verifying WebMCP actions
against real business state, requiring human approval, and turning silent failures
into replayable tests.

## Inspiration

WebMCP gives agents structured tools on real websites. That improves how an agent
acts, but it does not prove the business outcome is correct. A tool can receive the
right arguments, return a valid success response, and update the UI while the
underlying cart, booking, permission, or record remains wrong.

The response comes from the channel being tested. We wanted a second channel: an
independent witness that could say what actually happened.

## What it does

ActionWitness lets a person define an outcome contract, records an agent's WebMCP
journey, observes authoritative business state independently, and produces a
deterministic verdict.

The demo makes the gap visible. An agent searches for a mug, adds it to the cart,
and applies `SAVE20`. Every tool reports success. The real cart remains `$25.00`
instead of `$20.00`, so ActionWitness fails the run as
`false_success_or_state_mismatch`.

For consequential actions, the agent pauses on a server-issued confirmation that
is bound to the exact workspace, run, action, arguments, and expiry. A person
approves once or denies it. A failed run can then become a portable regression
case that restores its fixture and replays the same classification locally or in CI.

## How we built it

Python 3.12 owns contracts, deterministic evaluation, orchestration, persistence,
observation, evidence, replay, and the CLI. FastAPI and Pydantic validate the
service boundary; SQLite stores isolated workspace and run state. Evidence records
are append-only, canonically serialized, and hash-linked.

React and strict TypeScript provide the shared human-agent workspace. Every direct
WebMCP access is isolated in one browser adapter, which owns discovery,
registration, cleanup, cancellation, and result normalization. Target-specific
integrations implement public core protocols, so neither the core nor generic UI
contains commerce-specific verdict logic.

The repository also includes a standalone, deterministic Buggy Store with
injectable failure profiles. It talks to ActionWitness only through a versioned
HTTP adapter, preserving the same boundary a real target uses.

## How WebMCP makes it better

WebMCP is not decoration in ActionWitness. The agent uses the same application
surface a person is watching, and each tool call is recorded into the active run.
The available tools change with workflow state, protected calls can remain pending
while a person decides, and findings can be inspected or replayed by either side.

When WebMCP is missing, the application remains understandable and operable; it
reports the unavailable capability instead of pretending the tools were registered.

## Challenges we ran into

The hardest boundary was separating a tool's self-report from proof. Reusing a
successful tool response as observed state would make the demo easy and the
product meaningless, so the types, persistence model, adapters, and tests keep the
two sources distinct end to end.

Human confirmation introduced a second hard problem: approval could not be a
generic modal or a client-side flag. It had to be server issued, narrowly bound,
expiring, workspace isolated, and safe under cancellation and retry.

The third challenge was failure behavior. An unavailable observer, ambiguous
source, malformed imported report, stale polling response, or broken evidence
chain cannot disappear into a green result. Every one becomes an explicit
non-pass state.

## Accomplishments we are proud of

- A complete false-success journey where the tool says success and independent
  state proves it wrong.
- Human consent that visibly suspends the agent call and cannot be created,
  broadened, or approved by the agent itself.
- Portable regression cases that restore fixtures and replay source failures.
- Target-neutral core logic with architecture tests enforcing dependency direction.
- Strict boundary validation in Python and TypeScript, plus deterministic,
  network-independent release gates.
- A public, credential-free deployment with the workspace and demo target on one origin.

## What we learned

Structured tool use is not the same as verified outcome. The more convincing a
tool response looks, the more important it is that the final verdict comes from a
source the tool does not control.

We also learned that human-agent collaboration is clearest when ownership is part
of the state model: the workspace should always say whose turn it is, why, and what
safe action can advance the run.

## What's next

The near-term work is additional target adapters, clearer contract authoring, and
more portable evidence viewers. Optional Shopify development-store and live-model
evaluation modules will remain gated until their public boundaries and release
tests meet the same safety bar as the deterministic demo.

Production checkout, payments, arbitrary remote execution, hosted multi-tenancy,
and bulk operations are intentionally out of scope.

## Human, Codex, and model contribution

The human operator chose the product thesis, scope, architecture, safety
invariants, and release decisions. Codex accelerated implementation, testing,
review, and documentation. No LLM decides whether business state is correct;
ActionWitness's authoritative verdict is deterministic.

## Built with

WebMCP, Python 3.12, FastAPI, Pydantic, SQLite, HTTPX, React, TypeScript, Vite,
Vitest, Playwright, pytest, Ruff, uv, Docker, and Codex.

## Links

- Live workspace: <https://actionwitness.onrender.com>
- Demo storefront: <https://actionwitness.onrender.com/demo>
- Source: <https://github.com/ichiorca/actionwitness>
- Evidence: <https://github.com/ichiorca/actionwitness/blob/main/docs/SUBMISSION_EVIDENCE.md>
