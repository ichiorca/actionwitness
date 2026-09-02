/**
 * Narrowing the regression-eval payloads (§15.4, §24).
 *
 * As with `workspace.ts`, this module narrows and nothing more. In particular
 * it never merges the two results §24.3 keeps apart: an eval `status` says
 * whether the replay met its expectation, and `overall_result` says what the
 * target did. A reproduced failure is `status: passed` with
 * `overall_result: failed`, and a client that folded them into one field would
 * report the product's best evidence as a broken build.
 */

import { isRecord, optionalString, requireArray, requireRecord, requireString, request } from "./client";

/** §15.4's listing row, before any replay is read. */
export interface EvalCaseRow {
  readonly evalCaseId: string;
  readonly name: string;
  readonly contentHash: string;
  readonly sourceRunId: string;
}

/** One replay, with the two results kept separate. */
export interface EvalRunResult {
  readonly evalRunId: string;
  readonly status: string;
  readonly overallResult: string | null;
  readonly environment: string;
}

/** A case plus its latest replay, which is what the panel renders. */
export interface EvalCaseDetail extends EvalCaseRow {
  readonly latest: EvalRunResult | null;
}

function parseCaseRow(value: unknown): EvalCaseRow {
  const record = requireRecord(value, "eval case");
  return {
    evalCaseId: requireString(record["eval_case_id"], "eval_case_id"),
    name: requireString(record["name"], "name"),
    contentHash: requireString(record["content_hash"], "content_hash"),
    sourceRunId: optionalString(record["source_run_id"]) ?? "",
  };
}

export function parseEvalRun(value: unknown): EvalRunResult {
  const record = requireRecord(value, "eval run");
  return {
    evalRunId: requireString(record["eval_run_id"], "eval_run_id"),
    status: requireString(record["status"], "status"),
    // Null is a real value here — a replay that could not complete has no
    // target outcome — so it is preserved rather than defaulted to a verdict.
    overallResult: optionalString(record["overall_result"]),
    environment: requireString(record["environment"], "environment"),
  };
}

export function parseEvalCaseDetail(value: unknown): EvalCaseDetail {
  const record = requireRecord(value, "eval case");
  const latest = record["latest_run"];
  return {
    ...parseCaseRow(record),
    latest: isRecord(latest) ? parseEvalRun(latest) : null,
  };
}

export function parseEvalCases(value: unknown): readonly EvalCaseRow[] {
  const record = requireRecord(value, "eval cases");
  return requireArray(record["cases"], "cases").map(parseCaseRow);
}

export async function listEvalCases(signal?: AbortSignal): Promise<readonly EvalCaseRow[]> {
  return await request("/evals", {
    parse: parseEvalCases,
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function readEvalCase(
  evalCaseId: string,
  signal?: AbortSignal,
): Promise<EvalCaseDetail> {
  return await request(`/evals/${evalCaseId}`, {
    parse: parseEvalCaseDetail,
    ...(signal === undefined ? {} : { signal }),
  });
}

/**
 * Every case in this workspace, each with its latest replay.
 *
 * §15.4's listing carries no replay, so a case's latest result needs its own
 * read — but only once. `known` is what the caller already holds, and a case
 * found there is reused rather than re-read.
 *
 * That distinction is the whole point of the parameter. This runs on every
 * workspace-phase transition, and a workspace at `EVAL_CASES_PER_WORKSPACE`
 * would otherwise spend eleven requests per transition against FR-009's budget
 * of a hundred and twenty a minute — enough to start refusing the page's own
 * polling, which is a UI that goes stale because of a panel nobody was looking
 * at. A replayed case is refreshed by merging the replay's own response, so the
 * only reads here are for cases this client has never seen.
 *
 * A case whose detail cannot be read is kept without a replay rather than
 * dropped: a case that exists and cannot be described is still a case, and
 * hiding it would make it look deleted.
 */
export async function listEvalCaseDetails(
  known: readonly EvalCaseDetail[] = [],
  signal?: AbortSignal,
): Promise<readonly EvalCaseDetail[]> {
  const held = new Map(known.map((entry) => [entry.evalCaseId, entry]));
  const rows = await listEvalCases(signal);
  return await Promise.all(
    rows.map(async (row) => {
      const already = held.get(row.evalCaseId);
      if (already !== undefined) {
        return already;
      }
      try {
        return await readEvalCase(row.evalCaseId, signal);
      } catch {
        return { ...row, latest: null };
      }
    }),
  );
}

export async function replayEvalCase(
  evalCaseId: string,
  environment: string,
  signal?: AbortSignal,
): Promise<EvalRunResult> {
  return await request(`/evals/${evalCaseId}/runs`, {
    method: "POST",
    body: { environment },
    parse: parseEvalRun,
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function createEvalCase(runId: string, signal?: AbortSignal): Promise<string> {
  return await request(`/runs/${runId}/evals`, {
    method: "POST",
    parse: (value) => requireString(requireRecord(value, "eval")["eval_case_id"], "eval_case_id"),
    ...(signal === undefined ? {} : { signal }),
  });
}
