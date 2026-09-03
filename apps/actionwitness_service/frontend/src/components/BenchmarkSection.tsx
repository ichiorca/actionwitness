/**
 * The controls that make the dual-layer benchmark reachable (§9.9, §15.6, AC-16).
 *
 * `BenchmarkPanel` renders the matrix and always could; what did not exist was
 * any way for a person to *arrive* at one. A suite could only be created and a
 * report only imported by hand-writing HTTP, so the panel's own empty state —
 * "import a supported evaluator report" — described an action the product did
 * not offer. This section is that door: choose a suite, create one, import a
 * report into it.
 *
 * **Kept out of `panels.tsx` deliberately.** That file is already the largest in
 * the frontend and holds twelve presentational panels; the composition and the
 * file input belong beside the panel they serve rather than inside it.
 *
 * **The source kind is asked, never assumed.** AC-16 requires the application to
 * "never represent either as a live execution", so the operator states whether
 * these trials came from a recorded fixture or an external import, and the panel
 * repeats that claim at the top of the matrix.
 *
 * **The frozen intent manifest is the same story a second time.** FR-100's
 * freeze — "approved variants are frozen into the content-hashed benchmark
 * manifest before trials begin; generation is not rerun between repetitions" —
 * was implemented, tested, and unreachable: no route and no control, so a
 * requirement the product claims could not be exercised by anybody. The block
 * below is the missing half, and it renders the *server's* record of the freeze
 * rather than remembering what it just sent.
 *
 * **Drafting variants with the model fills the form; it never signs it.**
 * FR-100's sequence is generate → validate → screen → approve → freeze, and the
 * control added here performs only the first. What comes back lands in the same
 * rows a person would have typed, **unticked**, so approving is still an act
 * somebody performs one row at a time. A draft that arrived pre-approved would
 * record a decision nobody made — and the constitution is explicit that an agent
 * cannot approve the material it will then be measured against.
 *
 * The control is present whether or not a live backend is configured, and says
 * which. Hiding it when the module is off would make the page differ between
 * deployments in a way a reader cannot interpret; disabling it with a sentence
 * saying why leaves the hand-written path — the one that always works — exactly
 * where it was.
 */

import { useEffect, useId, useRef, useState } from "react";

import type {
  BenchmarkCorrelation,
  BenchmarkSummary,
  BenchmarkView,
  CorrelatedPopulation,
  DraftedVariants,
  Rate,
  VariantApprovalRequest,
  VariantDraft,
} from "../api/benchmark";
import {
  generateIntentVariants,
  readBenchmarkCorrelation,
  runRepeatedTrials,
} from "../api/benchmark";
import registry from "../generated/registry.json";
import { BenchmarkPanel } from "./panels";

export interface BenchmarkSectionProps {
  readonly benchmarks: readonly BenchmarkSummary[];
  readonly selectedId: string | null;
  readonly benchmark: BenchmarkView | null;
  readonly busy: boolean;
  /**
   * The `live_evaluator` module's state, as the workspace reports it.
   *
   * It gates nothing here, and that is deliberate. FR-100's freeze records a
   * *human* decision about texts; the module only says whether a model was
   * available to draft them. Hiding the control when the module is off would
   * leave the requirement unreachable in every default deployment — the exact
   * failure this section exists to fix — so the honest degradation is to keep
   * the control and say plainly that nothing here generated these variants.
   */
  readonly liveEvaluatorStatus: string;
  readonly liveEvaluatorReason: string;
  readonly onSelect: (benchmarkId: string) => void;
  readonly onCreate: (sourceKind: string, correlationMode: string) => void;
  readonly onImport: (report: string) => void;
  readonly onFreezeVariants: (approval: VariantApprovalRequest) => void;
  /**
   * FR-100's generate step, overridable for tests.
   *
   * Optional and defaulted to the real API call rather than threaded down from
   * `App`, because generation produces nothing the workspace has to hold: no
   * suite changes, no poll is invalidated, and the result lives in this form
   * until a person freezes it. A callback carried through the tree would be
   * plumbing for state that does not exist.
   */
  readonly onGenerateVariants?: (
    benchmarkId: string,
    canonicalIntent: string,
    count: number,
  ) => Promise<DraftedVariants>;
  readonly onReplay: () => void;
  readonly onFinalize: () => void;
  readonly trialHref: (externalTrialId: string) => string;
  readonly reportHref: string;
}

/** §16.4's two populations. Named by the tokens the API uses, because the
 *  generic UI does not translate a vocabulary the server owns. */
const CORRELATION_MODES = ["imported_trajectory_replay", "executed_browser"] as const;

/** AC-16's two honest provenances. There is deliberately no "live" option: this
 *  build imports reports and replays fixtures, and offering a third would let a
 *  screenshot claim something the harness did not do. */
const SOURCE_KINDS = ["recorded_fixture", "external_import"] as const;

/**
 * FR-100's three kinds and the server's own sentence for each, read from the
 * generated registry rather than retyped.
 *
 * The vocabulary belongs to the core; `tests/unit/test_registry.py` fails if
 * this artifact drifts from it. A hard-coded list here would be a second
 * spelling of a closed enum, and the freeze would start refusing the day a
 * fourth kind was added.
 */
const VARIANT_KIND_COPY: Readonly<Record<string, string>> = registry.enums.variant_kind.members;
const VARIANT_KINDS: readonly string[] = Object.keys(VARIANT_KIND_COPY);

/**
 * FR-100's ceiling, mirrored so the form stops offering a row the server would
 * refuse. The server is still the enforcer — it refuses a seventh variant
 * rather than truncating, because truncating would choose which variants a
 * human then approves — and this number only keeps the UI from proposing an
 * action that cannot succeed.
 */
const MAX_VARIANTS = 6;

/** One row of the review table: a variant, and whether the reviewer kept it. */
interface VariantRow extends VariantDraft {
  readonly approved: boolean;
}

function emptyRow(): VariantRow {
  // `paraphrased` is first in the registry and is the only kind that is always
  // meaningful; the reviewer changes it per row.
  return { kind: VARIANT_KINDS[0] ?? "paraphrased", text: "", approved: true };
}

export function BenchmarkSection({
  benchmarks,
  selectedId,
  benchmark,
  busy,
  liveEvaluatorStatus,
  liveEvaluatorReason,
  onSelect,
  onCreate,
  onImport,
  onFreezeVariants,
  onGenerateVariants = generateIntentVariants,
  onReplay,
  onFinalize,
  trialHref,
  reportHref,
}: BenchmarkSectionProps): React.ReactElement {
  const [sourceKind, setSourceKind] = useState<string>(SOURCE_KINDS[0]);
  const [correlationMode, setCorrelationMode] = useState<string>(CORRELATION_MODES[0]);
  const [readError, setReadError] = useState<string | null>(null);
  const [canonicalIntent, setCanonicalIntent] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [rows, setRows] = useState<readonly VariantRow[]>([emptyRow()]);
  const [freezeError, setFreezeError] = useState<string | null>(null);
  const [draftCount, setDraftCount] = useState(3);
  const [drafting, setDrafting] = useState(false);
  const [draftNote, setDraftNote] = useState<string | null>(null);
  const [draftError, setDraftError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const suiteId = useId();
  const sourceId = useId();
  const modeId = useId();
  const fileId = useId();
  const intentId = useId();
  const reviewerId = useId();
  const rowId = useId();
  const draftCountId = useId();

  const frozen = benchmark?.manifest.frozenVariants ?? null;
  // A suite that has left `draft` has trials, and FR-100 freezes "before trials
  // begin". The server refuses either way; saying so here means a person is not
  // offered a form that would be turned down.
  const sealable = benchmark !== null && benchmark.status === "draft";

  const liveBackendReady = liveEvaluatorStatus === "enabled";

  /**
   * Ask the model for candidates and put them in the rows, unticked.
   *
   * Existing rows are replaced rather than appended to. A reviewer who drafts
   * twice is asking for a different set, and mixing the two would produce a
   * list whose positions no longer match what they read — and positions are how
   * the approval names variants.
   */
  function draftVariants(): void {
    const intent = canonicalIntent.trim();
    if (selectedId === null || intent === "") {
      setDraftError("Write the canonical intent first: the model rephrases that sentence.");
      return;
    }
    setDraftError(null);
    setDraftNote(null);
    setDrafting(true);
    onGenerateVariants(selectedId, intent, draftCount).then(
      (drafted) => {
        setDrafting(false);
        // Ticked by nobody. The server says `approved: false` and this row
        // state says the same thing, so a reviewer has to make each decision
        // rather than un-make one somebody else pre-supplied.
        setRows(
          drafted.variants.length === 0
            ? [emptyRow()]
            : drafted.variants.map((variant) => ({ ...variant, approved: false })),
        );
        setDraftNote(
          drafted.variants.length === 0
            ? `${drafted.modelName} proposed no variants. Write your own, or draft again.`
            : `${String(drafted.variants.length)} candidates drafted by ${drafted.modelName} ` +
              `(${drafted.modelProvider}). None is approved: read each one and tick the ` +
              `ones you accept.`,
        );
      },
      (error: unknown) => {
        setDrafting(false);
        // The server's own sentence when it sent one — it distinguishes "no
        // backend configured" from "the model refused" from "the answer was
        // unusable", and a generic message here would throw that away.
        setDraftError(
          error instanceof Error ? error.message : "The model could not be reached.",
        );
      },
    );
  }

  function submitFreeze(): void {
    const intent = canonicalIntent.trim();
    const named = reviewer.trim();
    const variants = rows.map((row) => ({ kind: row.kind, text: row.text.trim() }));
    if (intent === "") {
      setFreezeError("Write the canonical intent the variants are variations of.");
      return;
    }
    if (named === "") {
      setFreezeError("Name the reviewer: the record is that a named person accepted these words.");
      return;
    }
    if (variants.some((variant) => variant.text === "")) {
      setFreezeError("Every variant needs text, or remove the empty row.");
      return;
    }
    setFreezeError(null);
    onFreezeVariants({
      canonicalIntent: intent,
      variants,
      // Approving nothing is a real decision — a reviewer who rejected all six
      // has done the job — so an empty selection is sent rather than refused.
      approvedIndices: rows.flatMap((row, index) => (row.approved ? [index] : [])),
      reviewer: named,
    });
  }

  return (
    <>
      <section className="panel" aria-label="Benchmark suites" id="panel-benchmarks" tabIndex={-1}>
        <h3>Benchmark suites</h3>
        <p className="panel__note">
          A benchmark pairs an external evaluator&rsquo;s call-level results with this
          harness&rsquo;s own outcome verdicts. The two layers are reported side by side and
          never merged into one score.
        </p>

        <div className="benchmark__controls">
          <label htmlFor={suiteId}>
            <span className="panel__label">Suite</span>
            <select
              id={suiteId}
              value={selectedId ?? ""}
              disabled={busy || benchmarks.length === 0}
              onChange={(event) => {
                onSelect(event.target.value);
              }}
            >
              {benchmarks.length === 0 ? <option value="">No suites yet</option> : null}
              {benchmarks.map((entry) => (
                <option key={entry.benchmarkId} value={entry.benchmarkId}>
                  {entry.benchmarkId} — {entry.status} ({entry.sourceKind})
                </option>
              ))}
            </select>
          </label>

          <label htmlFor={sourceId}>
            <span className="panel__label">Source kind</span>
            <select
              id={sourceId}
              value={sourceKind}
              disabled={busy}
              onChange={(event) => {
                setSourceKind(event.target.value);
              }}
            >
              {SOURCE_KINDS.map((kind) => (
                <option key={kind} value={kind}>
                  {kind}
                </option>
              ))}
            </select>
          </label>

          <label htmlFor={modeId}>
            <span className="panel__label">Correlation mode</span>
            <select
              id={modeId}
              value={correlationMode}
              disabled={busy}
              onChange={(event) => {
                setCorrelationMode(event.target.value);
              }}
            >
              {CORRELATION_MODES.map((mode) => (
                <option key={mode} value={mode}>
                  {mode}
                </option>
              ))}
            </select>
          </label>

          <button
            type="button"
            disabled={busy}
            onClick={() => {
              onCreate(sourceKind, correlationMode);
            }}
          >
            Create suite
          </button>
        </div>

        <div className="benchmark__controls">
          <label htmlFor={fileId}>
            <span className="panel__label">Evaluator report (JSON)</span>
            {/* Read in the browser and posted as the operator's own bytes: the
                route measures the raw body against FR-117's cap before parsing
                it, so re-serialising here would measure a different file. */}
            <input
              id={fileId}
              ref={fileInput}
              type="file"
              accept="application/json,.json"
              disabled={busy || selectedId === null}
            />
          </label>
          <button
            type="button"
            disabled={busy || selectedId === null}
            onClick={() => {
              const file = fileInput.current?.files?.[0];
              if (file === undefined) {
                setReadError("Choose a report file first.");
                return;
              }
              setReadError(null);
              file.text().then(
                (text) => {
                  onImport(text);
                },
                () => {
                  // A file the browser cannot read is the operator's to fix,
                  // and saying which half failed saves them checking the server.
                  setReadError("That file could not be read.");
                },
              );
            }}
          >
            Import report
          </button>
        </div>
        {readError === null ? null : (
          <p className="panel__error" role="status">
            {readError}
          </p>
        )}
      </section>

      <section className="panel" aria-label="Frozen intent manifest" tabIndex={-1}>
        <h3>Frozen intent manifest</h3>
        <p className="panel__note">
          Up to six approved variants of one canonical intent, sealed into this suite&rsquo;s
          content-hashed manifest before any trial runs. Sealed once: generation is not rerun
          between repetitions, so a different set is a different benchmark and needs a new suite.
        </p>

        {liveEvaluatorStatus === "enabled" ? null : (
          <p className="panel__note">
            No live model backend is configured
            {liveEvaluatorReason === "" ? "" : ` (${liveEvaluatorReason})`}, so nothing here
            generated these variants. Freeze only a set a person wrote and read.
          </p>
        )}

        {benchmark === null ? (
          <p className="panel__note">Choose or create a suite before freezing a variant set.</p>
        ) : frozen === null ? (
          <>
            <p>
              <span className="panel__label">Variant set:</span> <strong>Not frozen</strong>{" "}
              &mdash; no approved variants have been sealed into this manifest.
            </p>
            {sealable ? (
              <>
                <div className="benchmark__controls">
                  <label htmlFor={intentId}>
                    <span className="panel__label">Canonical intent</span>
                    <input
                      id={intentId}
                      type="text"
                      value={canonicalIntent}
                      disabled={busy}
                      onChange={(event) => {
                        setCanonicalIntent(event.target.value);
                      }}
                    />
                  </label>
                  <label htmlFor={reviewerId}>
                    <span className="panel__label">Reviewer</span>
                    <input
                      id={reviewerId}
                      type="text"
                      value={reviewer}
                      disabled={busy}
                      onChange={(event) => {
                        setReviewer(event.target.value);
                      }}
                    />
                  </label>
                </div>

                <div className="benchmark__controls">
                  <label htmlFor={draftCountId}>
                    <span className="panel__label">Candidates to draft</span>
                    <select
                      id={draftCountId}
                      value={String(draftCount)}
                      disabled={busy || drafting || !liveBackendReady}
                      onChange={(event) => {
                        setDraftCount(Number(event.target.value));
                      }}
                    >
                      {Array.from({ length: MAX_VARIANTS }, (_, index) => index + 1).map(
                        (option) => (
                          <option key={option} value={String(option)}>
                            {option}
                          </option>
                        ),
                      )}
                    </select>
                  </label>
                  <button
                    type="button"
                    disabled={busy || drafting || !liveBackendReady}
                    onClick={draftVariants}
                  >
                    Draft variants with the model
                  </button>
                  {/* State in words, never by the disabled colour alone (§8.4).
                      Three states, and a reader has to be able to tell them
                      apart: no backend, asking, and the ordinary case where the
                      only thing missing is a canonical intent. */}
                  <p className="panel__note" role="status">
                    {!liveBackendReady
                      ? "Drafting is unavailable: this deployment has no live model backend. Write the variants yourself below."
                      : drafting
                        ? "Asking the model…"
                        : "The model drafts candidates; approving them is still yours."}
                  </p>
                </div>
                {draftNote === null ? null : (
                  <p className="panel__note" role="status">
                    {draftNote}
                  </p>
                )}
                {draftError === null ? null : (
                  <p className="panel__error" role="status">
                    {draftError}
                  </p>
                )}

                {rows.map((row, index) => (
                  // Keyed by position on purpose: an approval names variants by
                  // position, so position *is* this row's identity.
                  <div className="benchmark__controls" key={`${rowId}-${String(index)}`}>
                    <label htmlFor={`${rowId}-kind-${String(index)}`}>
                      <span className="panel__label">Kind of variant {index + 1}</span>
                      <select
                        id={`${rowId}-kind-${String(index)}`}
                        value={row.kind}
                        disabled={busy}
                        onChange={(event) => {
                          const kind = event.target.value;
                          setRows((held) =>
                            held.map((entry, at) => (at === index ? { ...entry, kind } : entry)),
                          );
                        }}
                      >
                        {VARIANT_KINDS.map((kind) => (
                          <option key={kind} value={kind}>
                            {kind}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label htmlFor={`${rowId}-text-${String(index)}`}>
                      <span className="panel__label">Variant {index + 1}</span>
                      <input
                        id={`${rowId}-text-${String(index)}`}
                        type="text"
                        value={row.text}
                        disabled={busy}
                        onChange={(event) => {
                          const text = event.target.value;
                          setRows((held) =>
                            held.map((entry, at) => (at === index ? { ...entry, text } : entry)),
                          );
                        }}
                      />
                    </label>
                    <label htmlFor={`${rowId}-approved-${String(index)}`}>
                      <input
                        id={`${rowId}-approved-${String(index)}`}
                        type="checkbox"
                        checked={row.approved}
                        disabled={busy}
                        onChange={(event) => {
                          const approved = event.target.checked;
                          setRows((held) =>
                            held.map((entry, at) =>
                              at === index ? { ...entry, approved } : entry,
                            ),
                          );
                        }}
                      />
                      <span className="panel__label">Approve variant {index + 1}</span>
                    </label>
                    <button
                      type="button"
                      disabled={busy || rows.length === 1}
                      onClick={() => {
                        setRows((held) => held.filter((_, at) => at !== index));
                      }}
                    >
                      Remove variant {index + 1}
                    </button>
                    <p className="panel__note">{VARIANT_KIND_COPY[row.kind] ?? row.kind}</p>
                  </div>
                ))}

                <div className="benchmark__controls">
                  <button
                    type="button"
                    disabled={busy || rows.length >= MAX_VARIANTS}
                    onClick={() => {
                      setRows((held) => [...held, emptyRow()]);
                    }}
                  >
                    Add variant
                  </button>
                  <button type="button" disabled={busy} onClick={submitFreeze}>
                    Freeze variant set
                  </button>
                </div>
                {freezeError === null ? null : (
                  <p className="panel__error" role="status">
                    {freezeError}
                  </p>
                )}
              </>
            ) : (
              <p className="panel__note">
                This suite is {benchmark.status}. Variants are frozen before trials begin, so a
                set for this benchmark would have to be sealed into a new suite.
              </p>
            )}
          </>
        ) : (
          <>
            <p>
              <span className="panel__label">Variant set:</span> <strong>Frozen</strong> &mdash;{" "}
              {frozen.variants.length} approved by {frozen.reviewer} ({frozen.actor}).
            </p>
            <dl className="benchmark__manifest">
              <dt>Manifest identity</dt>
              <dd>{benchmark.manifestContentHash ?? "not reported"}</dd>
              <dt>Canonical intent</dt>
              <dd>{frozen.canonicalIntent}</dd>
              <dt>Approved at</dt>
              <dd>{frozen.approvedAt}</dd>
            </dl>
            {frozen.variants.length === 0 ? (
              <p className="panel__note">
                The reviewer approved none of the generated variants. This suite runs the
                canonical intent alone &mdash; which is a decision somebody made, not an
                absence.
              </p>
            ) : (
              <ul className="benchmark__breakdown">
                {frozen.variants.map((variant) => (
                  <li key={variant.text}>
                    <span className="panel__label">{variant.kind}:</span> {variant.text}
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </section>

      <RepeatedTrialsSection selectedId={selectedId} benchmark={benchmark} busy={busy} />

      <BenchmarkPanel
        benchmark={benchmark}
        busy={busy}
        onReplay={onReplay}
        onFinalize={onFinalize}
        trialHref={trialHref}
        reportHref={reportHref}
      />
    </>
  );
}

/* -- repeated trials and the correlation they produce (§26.5, §9.9) -------- */

export interface RepeatedTrialsSectionProps {
  readonly selectedId: string | null;
  readonly benchmark: BenchmarkView | null;
  readonly busy: boolean;
}

/** A rate as words, or the reason there is no answer. Never `0` for `null`. */
function rateText(rate: Rate): string {
  return rate.value ?? "no population";
}

/**
 * The disagreement cell, in a sentence.
 *
 * §8.4 forbids colour as the only status channel, and this cell is the one
 * finding the whole product exists to surface — a reader must be able to take it
 * from the words alone. The three branches are three different facts, and the
 * middle one is the trap: a rate of `null` means nothing passed at call level,
 * which is not the same as "nothing disagreed".
 */
function disagreementSentence(population: CorrelatedPopulation): string {
  const passes = population.overstatedRate.denominator;
  if (population.overstatedRate.value === null) {
    return (
      `No trial of this variant passed at call level, so there is nothing for the ` +
      `observed state to contradict yet.`
    );
  }
  if (population.overstatedTrials === 0) {
    return (
      `Every one of the ${String(passes)} calls the evaluator scored correct also left ` +
      `business state this harness judged correct.`
    );
  }
  return (
    `The evaluator scored ${String(passes)} of these calls correct; ` +
    `${String(population.overstatedTrials)} of them left business state this harness judged ` +
    `wrong. That is a silent-failure rate of ${String(population.overstatedRate.value)} — ` +
    `these are the trials an evaluator alone would have passed.`
  );
}

/**
 * Run one variant N times, and read the two layers side by side.
 *
 * **Fetched here rather than passed in.** The correlation view changes when a
 * batch runs and at no other time, and threading it through the workspace's
 * polling loop would refetch it on every tick for every reader who is not
 * looking at a benchmark.
 *
 * **Every fetch is abortable and stale results are dropped.** StrictMode runs
 * setup, cleanup, setup, so the first request is aborted while in flight; a
 * component that let it land would render a view for a suite the operator has
 * already moved away from.
 *
 * **The ceiling is the server's number.** It arrives with the view and bounds
 * the input, so the form stops offering a batch the server would refuse without
 * this bundle keeping a second copy free to drift.
 */
export function RepeatedTrialsSection({
  selectedId,
  benchmark,
  busy,
}: RepeatedTrialsSectionProps): React.ReactElement {
  const [correlation, setCorrelation] = useState<BenchmarkCorrelation | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [sourceTrialId, setSourceTrialId] = useState("");
  const [count, setCount] = useState("3");
  const [variantIndex, setVariantIndex] = useState("");
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const sourceId = useId();
  const countId = useId();
  const variantId = useId();

  const trials = benchmark?.trials ?? [];
  const trialCount = trials.length;

  useEffect(() => {
    // A suite with no trials has nothing to correlate, and the server would say
    // so at the cost of a request. Not asking is not an optimisation: it keeps
    // the empty state a fact this component already knows rather than one it
    // reports on the server's behalf.
    if (selectedId === null || trialCount === 0) {
      setCorrelation(null);
      setLoadError(null);
      return;
    }
    const controller = new AbortController();
    let current = true;
    readBenchmarkCorrelation(selectedId, controller.signal).then(
      (view) => {
        if (!current) {
          return;
        }
        setCorrelation(view);
        setLoadError(null);
      },
      (error: unknown) => {
        if (!current) {
          return;
        }
        setCorrelation(null);
        setLoadError(
          error instanceof Error ? error.message : "The correlation view could not be read.",
        );
      },
    );
    return () => {
      current = false;
      controller.abort();
    };
  }, [selectedId, trialCount, reloadToken]);

  const ceiling = correlation?.repetitionCeiling ?? 0;
  const variants = benchmark?.manifest.frozenVariants?.variants ?? [];
  const canRun = !busy && !running && selectedId !== null && correlation !== null;

  function submit(): void {
    if (selectedId === null || correlation === null) {
      return;
    }
    const chosen = sourceTrialId === "" ? (trials[0]?.externalTrialId ?? "") : sourceTrialId;
    if (chosen === "") {
      setRunError("Choose the trial whose recorded journey should be run again.");
      return;
    }
    const requested = Number.parseInt(count, 10);
    if (!Number.isInteger(requested) || requested < 1 || requested > ceiling) {
      setRunError(`Ask for between 1 and ${String(ceiling)} trials.`);
      return;
    }
    setRunError(null);
    setNotice(null);
    setRunning(true);
    runRepeatedTrials(selectedId, {
      sourceExternalTrialId: chosen,
      trials: requested,
      ...(variantIndex === "" ? {} : { variantIndex: Number.parseInt(variantIndex, 10) }),
    }).then(
      (receipt) => {
        setRunning(false);
        // What was recorded, not what was asked for. A batch that stopped early
        // must not be reported as the batch the operator requested.
        setNotice(`${String(receipt.trials)} trials recorded.`);
        setReloadToken((token) => token + 1);
      },
      (error: unknown) => {
        setRunning(false);
        setRunError(error instanceof Error ? error.message : "Those trials could not be run.");
        // Reloaded even on failure: a batch that refused partway through still
        // left the repetitions it had already recorded, and hiding them would
        // make a partial run look like no run at all.
        setReloadToken((token) => token + 1);
      },
    );
  }

  return (
    <section className="panel" aria-label="Repeated trials and correlation" tabIndex={-1}>
      <h3>Repeated trials</h3>
      <p className="panel__note">
        One sample says what happened once. Running the same intent several times says how
        often &mdash; and the table below sets the evaluator&rsquo;s own verdicts against what
        this harness independently observed, so the cell where they disagree is a rate rather
        than an anecdote.
      </p>

      {selectedId === null ? (
        <p className="panel__note">Choose or create a suite before running repeated trials.</p>
      ) : trialCount === 0 ? (
        <p className="panel__note">
          This suite holds no trials yet. Import an evaluator report first &mdash; a repetition
          runs a journey that was already recorded, and there is nothing here to run again.
        </p>
      ) : loadError !== null ? (
        <p className="panel__error" role="status">
          {loadError}
        </p>
      ) : correlation === null ? (
        <p className="panel__note">Reading this suite&rsquo;s correlation&hellip;</p>
      ) : (
        <>
          {correlation.evaluatorImportAvailable ? null : (
            <p className="panel__note">
              Evaluator import is switched off in this deployment, so no call-level verdict can
              reach this suite. Repetitions would still record what this harness observes, but
              there is no second opinion here to correlate them against.
            </p>
          )}

          <div className="benchmark__controls">
            <label htmlFor={sourceId}>
              <span className="panel__label">Trial to run again</span>
              <select
                id={sourceId}
                value={sourceTrialId}
                disabled={busy || running || trials.length === 0}
                onChange={(event) => {
                  setSourceTrialId(event.target.value);
                }}
              >
                {trials.length === 0 ? <option value="">No trials imported yet</option> : null}
                {trials.map((trial) => (
                  <option key={trial.externalTrialId} value={trial.externalTrialId}>
                    {trial.externalTrialId}
                  </option>
                ))}
              </select>
            </label>

            <label htmlFor={countId}>
              <span className="panel__label">Trials</span>
              <input
                id={countId}
                type="number"
                min={1}
                max={ceiling}
                value={count}
                disabled={busy || running}
                onChange={(event) => {
                  setCount(event.target.value);
                }}
              />
            </label>

            {variants.length === 0 ? null : (
              <label htmlFor={variantId}>
                <span className="panel__label">Frozen variant</span>
                <select
                  id={variantId}
                  value={variantIndex}
                  disabled={busy || running}
                  onChange={(event) => {
                    setVariantIndex(event.target.value);
                  }}
                >
                  {/* An unnamed variant is a real choice: a suite may repeat a
                      trial that exercised no frozen variant, and the view then
                      groups it by scenario rather than claiming it ran one. */}
                  <option value="">No variant &mdash; group by scenario</option>
                  {variants.map((variant, index) => (
                    <option key={variant.text} value={String(index)}>
                      {variant.kind}: {variant.text}
                    </option>
                  ))}
                </select>
              </label>
            )}

            <button type="button" disabled={!canRun} onClick={submit}>
              Run repeated trials
            </button>
          </div>
          <p className="panel__note">
            Up to {ceiling} trials per batch, as this server states it. Each one runs in its own
            isolated workspace and is recorded separately, so an interrupted batch keeps the
            trials it had already run.
          </p>
          {runError === null ? null : (
            <p className="panel__error" role="status">
              {runError}
            </p>
          )}
          {notice === null ? null : (
            <p className="panel__note" role="status">
              {notice}
            </p>
          )}

          {correlation.populations.length === 0 ? (
            <p className="panel__note">
              No trials have run in this suite yet, so there is nothing to correlate. This is an
              absence of measurement, not a measurement of agreement.
            </p>
          ) : (
            correlation.populations.map((population) => (
              <CorrelationMatrix key={population.label} population={population} />
            ))
          )}
        </>
      )}
    </section>
  );
}

/** One population's two-by-two, its rates, and what the signal cell means. */
function CorrelationMatrix({
  population,
}: {
  readonly population: CorrelatedPopulation;
}): React.ReactElement {
  const counts = population.counts;
  return (
    <>
      {/* Its own scroll container: a wide matrix must scroll inside itself
          rather than pushing the page sideways. A class, not a `style=`
          attribute — the CSP's `style-src 'self'` refuses those. */}
      <div className="benchmark__scroll">
        <table className="benchmark__matrix">
          <caption>
            {population.label} &mdash; {population.trials} trials recorded,{" "}
            {counts.eligibleTrials} counted, {counts.excludedTrials} excluded
          </caption>
          <thead>
            <tr>
              <th scope="col">Evaluator said</th>
              <th scope="col">Observed state passed</th>
              <th scope="col">Observed state failed</th>
            </tr>
          </thead>
          <tbody>
            <tr className={population.overstatedTrials > 0 ? "benchmark__row--signal" : undefined}>
              <th scope="row">Call passed</th>
              <td>{counts.callLevelPassOutcomePass}</td>
              <td>
                {counts.callLevelPassOutcomeFail} ({rateText(population.overstatedRate)} of
                call-level passes)
              </td>
            </tr>
            <tr>
              <th scope="row">Call failed</th>
              <td>
                {counts.callLevelFailOutcomePass} ({rateText(population.understatedRate)} of
                call-level failures)
              </td>
              <td>{counts.callLevelFailOutcomeFail}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <p className="panel__note">{disagreementSentence(population)}</p>

      <dl className="benchmark__metrics">
        <dt>Layers agreed</dt>
        <dd>
          {population.agreementTrials} trials ({rateText(population.agreementRate)})
        </dd>
        <dt>End-to-end success</dt>
        <dd>{rateText(population.metrics.endToEndSuccessRate)}</dd>
        <dt>Observed outcomes</dt>
        <dd>
          {population.observedDistribution
            .filter((entry) => entry.trials > 0)
            .map((entry) => `${entry.result} ${String(entry.trials)}`)
            .join(", ") || "none recorded"}
        </dd>
        <dt>Evaluator verdicts</dt>
        <dd>
          {population.evaluatorDistribution
            .filter((entry) => entry.trials > 0)
            .map((entry) => `${entry.result} ${String(entry.trials)}`)
            .join(", ") || "none recorded"}
        </dd>
      </dl>
    </>
  );
}
