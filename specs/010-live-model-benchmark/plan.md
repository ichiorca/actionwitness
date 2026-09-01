# 010 — plan

Follow the spec's normative order exactly; each stage lands with its tests
before the next begins (constitution: an untested boundary is not implemented).

## What makes this milestone different

**This one is mostly a proof that 008 was built honestly.** M7 delivered import,
binding, replay, correlation, and the artifact. M9 changes exactly one thing:
where the report came from. If any stage here needs a second import path, a
second matrix, or a special case in the correlation engine, that is evidence the
Tier 2 path was fixture-shaped after all — and the finding is worth more than
the workaround.

**The model is on the far side of a boundary the engine never crosses.** FR-099
is unambiguous: "the deterministic contract engine shall make no model call."
The model generates candidate intents and drives the evaluator; the engine
observes authoritative state. A single call from the engine would make the
product's central claim circular, because the thing under test would be
producing the evidence about itself.

**Credentials live in one process and nowhere else.** FR-099 names four places
they must never be: the browser, a WebMCP argument, a committed file, an
uploaded manifest. AC-17 adds the positive form — retained "only in the
evaluator process environment". This is the milestone where a careless
convenience (echoing configuration into a report, logging the resolved backend)
becomes a leaked key in a committed artifact.

**Generation is reviewed, then frozen, then never rerun.** FR-100's sequence is
load-bearing. Variants that regenerate between repetitions would mean the
repetitions measured different things, and the manifest's content hash would
describe none of them. Human approval sits *between* generation and freezing,
so nobody can approve a set that later changed.

**A variant is untrusted text.** FR-100 requires rejecting variants "containing
secrets or instructions to bypass confirmation". These are model-authored
strings that become benchmark inputs; treating them as data rather than as
instructions is the same rail §5 applies to every other external input.

**The fallback must stay honest under pressure.** FR-101 and §25.3 both insist
the checked-in fixture is labeled `recorded_fixture` and "never presented as a
live execution" — and the moment that matters is a demo where the credential or
quota has failed. 008's panel already labels the source kind; this milestone
must not add a path that quietly relabels it.

**Nothing here is required for a Tier 2 release.** The entry condition is M6,
M7, and AC-16 green, and the exit gate is a hard stop: if AC-17 does not pass,
Shopify work does not begin.

---

1. **The configured backend.** One explicitly configured LLM backend behind a
   pinned `webmcp-evals` configuration, resolved from the environment like every
   other module. Absent configuration disables the live path and leaves the
   Tier 2 import path untouched (FR-096 already requires that separation).
2. **Credential handling.** Supplied by developer environment or deployment
   secret; held in the evaluator process environment; never in a browser, a
   WebMCP argument, a committed file, or an uploaded manifest. A test that
   reaches for it in each of those places and finds nothing.
3. **Intent generation.** Up to six variants from one canonical contract
   intent — paraphrased, ambiguous, adversarial. Python schema-validates length
   and character limits.
4. **Variant screening.** Reject variants carrying secrets or instructions to
   bypass confirmation, before a human is asked to review them.
5. **Human approval.** Explicit, recorded, and prior to freezing.
6. **Freezing.** Approved variants hashed into the benchmark manifest before
   trials begin; generation is not rerun between repetitions.
7. **Live trials.** At least three scenarios with at least three completed live
   trials each, through the M7 import path unchanged.
8. **Parameter capture.** Exported model and evaluator parameters persisted
   exactly; unsupported values `null`, never inferred.
9. **The `live_model_run` artifact.** Finalized and precomputed before any
   demo recording, with its source kind never interchangeable with a fixture.
10. **Offline fallback.** The checked-in fixture keeps the matrix UI and
    deterministic verification reproducible with no credential, quota, or
    network, and stays labeled `recorded_fixture`.
11. **The exit gate.** AC-17 through the same pipeline; the CI fixture path
    still green; the credential provably confined; and the hard stop on Shopify
    work if any of it fails.

---

## Carried forward from 008

Three deviations recorded in `specs/008-evaluator-import/plan.md` bear directly
on this milestone, and a live report is the thing that settles the first:

- **D1 — the imported trajectory element shape is unverified upstream.** 008
  assumed `{name, arguments}` for a trajectory step because ADR-0005 never
  recorded the element type. **The first real report this milestone produces is
  the check.** If it disagrees, the fix is confined to `_trajectory` in
  `integrations/google_evals/normalize.py` and the checked-in fixture — but
  until then, AC-16's replay half rests on a shape this project chose.
- **D2 — scenarios carry the target configuration, not the report.** A live
  report will not carry scenario mode or failure profile either, so the
  manifest's `ScenarioDefinition` remains how a live suite declares them.
- **D3 — one contract judges every scenario in a suite.** FR-100's six variants
  are variants of *one canonical contract intent*, so a single contract per
  suite is compatible with this milestone. It stops being compatible the moment
  a suite spans intents, which is where per-scenario contracts would have to
  land.

---

## Deviations ledger (implementation)

Each departure from the spec, anchored to the section it departs from, with what
was taken and why.

_No entries yet; implementation has not begun._
