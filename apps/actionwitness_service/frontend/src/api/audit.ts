/**
 * The external-surface audit, from the browser (§12.17, FR-160, FR-161).
 *
 * Narrowing and requests only. Every judgement — which tools were expected,
 * what a claim beside an observation means, what the merchant report says —
 * belongs to the server; this module carries the operator's transcript there
 * and brings a report back.
 *
 * **The transcript is gathered outside this page, and that is not a shortcut.**
 * §12.17 puts the observation in the operator's own browser, and a document can
 * only enumerate *its own* `modelContext` and read *its own* session's
 * `cart.js`. A harness page therefore cannot reach into an audited storefront
 * to collect either, and any design where it appeared to would mean the server
 * had started contacting the audited origin — the one thing the audit is built
 * never to do. So the operator runs the collector on the storefront and brings
 * the result here.
 */

import { isRecord, optionalString, request, requireArray, requireRecord, requireString } from "./client";

/** One built-in contract pack, as the catalogue offers it (FR-161). */
export interface AuditPack {
  readonly packId: string;
  readonly title: string;
  /** What a surface must publish for this pack to apply. */
  readonly signature: readonly string[];
  /** Reported as present and never exercised (FR-162). */
  readonly neverInvoked: readonly string[];
}

/** The workspace's live audit, or the absence of one. */
export interface LiveAudit {
  readonly auditId: string;
  readonly authorizedOrigin: string;
  readonly assertedBy: string;
  readonly assertedAt: string;
  readonly status: string;
}

function parseAudit(value: unknown, field: string): LiveAudit {
  const record = requireRecord(value, field);
  return {
    auditId: requireString(record["audit_id"], `${field}.audit_id`),
    authorizedOrigin: requireString(record["authorized_origin"], `${field}.authorized_origin`),
    assertedBy: requireString(
      record["authorization_asserted_by"],
      `${field}.authorization_asserted_by`,
    ),
    assertedAt: requireString(
      record["authorization_asserted_at"],
      `${field}.authorization_asserted_at`,
    ),
    status: requireString(record["status"], `${field}.status`),
  };
}

function parseStringList(value: unknown, field: string): readonly string[] {
  return requireArray(value, field).map((entry, index) =>
    requireString(entry, `${field}[${String(index)}]`),
  );
}

export async function listAuditPacks(signal?: AbortSignal): Promise<readonly AuditPack[]> {
  return await request("/audits/packs", {
    parse: (value) =>
      requireArray(requireRecord(value, "packs")["packs"], "packs").map((entry, index) => {
        const record = requireRecord(entry, `packs[${String(index)}]`);
        return {
          packId: requireString(record["pack_id"], "pack_id"),
          title: requireString(record["title"], "title"),
          signature: parseStringList(record["signature"], "signature"),
          neverInvoked: parseStringList(record["never_invoked"], "never_invoked"),
        };
      }),
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function readCurrentAudit(signal?: AbortSignal): Promise<LiveAudit | null> {
  return await request("/audits/current", {
    parse: (value) => {
      const audit = requireRecord(value, "current")["audit"];
      // `null` is an answer, not an absence: the workspace exists and has no
      // live audit, which is different from "that is not yours".
      return isRecord(audit) ? parseAudit(audit, "audit") : null;
    },
    ...(signal === undefined ? {} : { signal }),
  });
}

/**
 * Assert authorization for exactly one origin (FR-160).
 *
 * `authorized` is sent as the operator's explicit affirmation rather than
 * implied by the call: the server refuses a submission that does not carry it,
 * and a client that always sent `true` would be authorizing on their behalf.
 */
export async function assertAuthorization(
  origin: string,
  assertedBy: string,
  authorized: boolean,
  signal?: AbortSignal,
): Promise<LiveAudit> {
  return await request("/audits", {
    method: "POST",
    body: { origin, asserted_by: assertedBy, authorized },
    parse: (value) => parseAudit(value, "audit"),
    ...(signal === undefined ? {} : { signal }),
  });
}

export interface AuditSubmission {
  readonly packId: string;
  readonly enumerated: readonly string[];
  readonly reports: Record<string, Record<string, unknown>>;
  readonly observedBefore: Record<string, unknown> | null;
  readonly observedAfter: Record<string, unknown> | null;
}

export interface AuditOutcome {
  readonly reportArtifactId: string;
  readonly contentHash: string;
  readonly report: Record<string, unknown>;
}

export async function submitAuditEvidence(
  submission: AuditSubmission,
  signal?: AbortSignal,
): Promise<AuditOutcome> {
  return await request("/audits/current/evidence", {
    method: "POST",
    body: {
      pack_id: submission.packId,
      enumerated: submission.enumerated,
      reports: submission.reports,
      observed_before: submission.observedBefore,
      observed_after: submission.observedAfter,
    },
    parse: (value) => {
      const record = requireRecord(value, "evidence");
      return {
        reportArtifactId: requireString(record["report_artifact_id"], "report_artifact_id"),
        contentHash: requireString(record["content_hash"], "content_hash"),
        report: requireRecord(record["report"], "report"),
      };
    },
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function readAuditReport(signal?: AbortSignal): Promise<AuditOutcome> {
  return await request("/audits/current/report", {
    parse: (value) => {
      const record = requireRecord(value, "report");
      return {
        reportArtifactId: requireString(record["report_artifact_id"], "report_artifact_id"),
        contentHash: optionalString(record["content_hash"]) ?? "",
        report: requireRecord(record["report"], "report"),
      };
    },
    ...(signal === undefined ? {} : { signal }),
  });
}

export async function cancelAudit(signal?: AbortSignal): Promise<void> {
  await request("/audits/current/cancel", {
    method: "POST",
    parse: () => undefined,
    ...(signal === undefined ? {} : { signal }),
  });
}
