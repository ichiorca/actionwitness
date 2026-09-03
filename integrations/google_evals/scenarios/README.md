# AC-17 live run — the suite and the runbook

The suite (`save20_suite.json`) is this repository's three canonical scenarios,
authored for the pinned `webmcp-evals@0.0.4` (ADR-0005) in **browser** mode.
The case names are load-bearing: they must match the `scenario_id`s the
benchmark suite is created with, byte for byte, or binding fails closed as
unbound (which is correct, and useless).

| Case | Scenario declared at suite creation | Expected shape |
|---|---|---|
| `SAVE20 on one mug against the faulty build` | `pre_fix` + `discount_reported_but_not_applied` | call pass · outcome fail — the silent defect |
| `SAVE20 on one mug against the corrected build` | `post_fix` | call pass · outcome pass |
| `SAVE20 on one mug, discount step omitted` | `post_fix` | call fail (the prompt forbids the discount `expectedCall` requires) |

`expectedCall` deliberately lists only the three target-tool calls. The prompt
also has the agent create the contract, arm, and verify — those are harness
tools, and FR-091 imports only the **allowlisted** target calls as the
replayable trajectory, so the transform below keeps target calls and drops the
rest (stated here, not hidden: the evaluator's own report remains the full
record).

## Running it (developer-executed — AC-17's own wording; the harness never runs it)

Prerequisites:

- The deployment's workspace page reachable in a browser. Local dev:
  `uv run buggy-store` + `uv run uvicorn actionwitness_service.api.app:create_app
  --factory --port 8000` + `npm run dev` in the harness frontend, page at
  `http://localhost:5173`. (The Docker container serves the same page at
  `http://localhost:8000/`.)
- Chrome installed. The evaluator launches its own Puppeteer instance with
  `--enable-features=WebMCP`; pass `--chrome-channel chrome` to use stable.
- The credential: the evaluator's `gemini` backend reads **`GOOGLE_AI`** from
  its own process environment via dotenv — run it from a directory whose
  `.env` carries that key (FR-099: the harness process never holds it).

```bash
npx webmcp-evals@0.0.4 \
  --chrome-channel chrome \
  -b gemini -m gemini-2.5-flash -r 3 \
  --reporter json console \
  -o .evals-live \
  browser -u http://localhost:5173 \
  -e integrations/google_evals/live_suite/save20_suite.json
```

Mind FR-009 while running — measured, not hypothetical (smoke runs,
2026-09-03): every page the evaluator opens is one anonymous workspace (ten
creations an hour per client) and **all loopback traffic shares one
per-minute bucket**. The workspace page polls at ~1 Hz and the timeline adds
another stream while a run is live, so *one* open workspace tab consumes
roughly half the budget and the evaluator's own page tips it over — the
observed failures are the page booting without its declarative tool (its
workspace read answered 429) and tool calls refused mid-journey. Close every
other workspace tab before a run, space invocations by minutes, and for
sustained work use the e2e lane's recipe — `HARNESS_TRUSTED_PROXIES` plus a
proxy stamping a distinct client address per caller
(`apps/actionwitness_service/frontend/e2e/README.md`). Do not touch the
limits (constitution §5).

Two more findings from the credential-free smoke validation:

- The **declarative** `create_outcome_contract` executed successfully under
  the evaluator's headless Chrome once, but its registration (the browser
  scanning the form that mounts only after the first workspace read) races
  the evaluator's five-second tool poll and usually loses in smoke mode.
  Browser mode re-enumerates tools on every agent turn, which tolerates the
  race; the suite's prompts still put `create_outcome_contract` first so a
  slow registration costs one wasted turn, not the trial.
- `smoke` writes no JSON report (console only). ADR-0005 keeps smoke out of
  the matrix anyway — use it purely to check the page and the journey wiring.

## Importing the report

The importer accepts exactly the shape of
`integrations/google_evals/fixtures/tier2_three_scenarios.json`
(`config.reporterSchema` must be `webmcp-evals/0.0.4`;
`results.results[]` rows of `{test:{name}, outcome, runIndex, response,
trajectory:[{name, arguments}]}`). The evaluator's raw JSON already matches
the row shape (verified against `webmcp-evals@0.0.4`'s own reporter source),
but its `config` block is the CLI invocation rather than the pinned header,
and its trajectories carry harness calls the replayer would refuse. Run

```bash
python integrations/google_evals/live_suite/transform_report.py \
  .evals-live/report-<ts>.json report-import.json \
  --model gemini-2.5-flash --commit "$(git rev-parse HEAD)"
```

which replaces the header and reduces each trajectory to the adapter's five
target tools (both stated in that script's docstring — the raw report stays
the full record). Then, with the module configured
(`LIVE_EVALUATOR_ENABLED`, `LIVE_EVALUATOR_PROVIDER=google`,
`LIVE_EVALUATOR_MODEL`, `LIVE_EVALUATOR_CREDENTIAL_VAR=GOOGLE_AI` — the
*name*, never the value):

1. `POST /api/v1/benchmarks` — `{"source_kind": "live_model_run", "scenarios": [...]}`
   with the three scenario definitions above (ids = case names).
2. `POST /api/v1/benchmarks/{id}/imports` — the transformed report, raw JSON body.
3. `PUT  /api/v1/benchmarks/{id}/bindings` — `{"seal": true}`.
4. `POST /api/v1/benchmarks/{id}/replay` — the deterministic outcome layer.
5. `POST /api/v1/benchmarks/{id}/finalize`, then `GET /api/v1/benchmarks/{id}`.

`source_kind_for` decides `live_model_run` from the resolved configuration —
a deployment without the module configured labels the suite `recorded_fixture`
whatever the caller asked for, which is the honest fallback, not a bug.

**Nothing in `specs/011-shopify-cart-proof/tasks.md` or the AC-17 row is
ticked until the matrix above is real** — `test_gate_6_shopify_work_has_not_
started` enforces the former mechanically.
