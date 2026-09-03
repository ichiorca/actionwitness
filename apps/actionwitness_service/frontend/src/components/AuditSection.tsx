/**
 * Auditing a storefront you did not build (§12.17, FR-160–FR-163, spec 015).
 *
 * The server half has been complete and tested for a while; this is the half a
 * person uses. Four steps, in the order §12.17 requires them: assert
 * authorization for one origin, choose a pack explicitly, collect the evidence
 * *in the operator's own browser*, and read the report.
 *
 * ## Why the collection is a snippet and not a button
 *
 * A document can enumerate only its **own** `modelContext`, and `cart.js` is a
 * read of the caller's **own** session. Neither crosses an origin. So there is
 * no arrangement in which this page reaches into an audited storefront and
 * gathers a transcript — and the one design that would appear to, a server-side
 * fetch, is precisely what §12.17 forbids and what
 * `tests/architecture/test_audit_guardrails.py` fails the build over.
 *
 * The honest shape is therefore: the harness generates a collector, the
 * operator runs it on the storefront they are authorized on, and brings the
 * transcript back. That is the same trust boundary the whole feature rests on —
 * the only party talking to the audited site is the person who already has an
 * account there.
 *
 * ## The snippet is generated from the chosen pack, never hand-written
 *
 * FR-162 makes `proceed_to_checkout` and `manage_orders` present-but-never
 * invoked, and the generated collector encodes that list rather than trusting
 * whoever pastes it to remember. Exercising a checkout tool against a real
 * storefront would create a real order for a real customer.
 */

import { useId, useMemo, useState } from "react";

import type { AuditOutcome, AuditPack, LiveAudit } from "../api/audit";
import { collectorFor } from "../webmcp/auditCollector";

export interface AuditSectionProps {
  readonly moduleStatus: string;
  readonly moduleReason: string;
  readonly packs: readonly AuditPack[];
  readonly audit: LiveAudit | null;
  readonly outcome: AuditOutcome | null;
  readonly busy: boolean;
  readonly onAuthorize: (origin: string, assertedBy: string) => void;
  readonly onSubmit: (packId: string, transcript: string) => void;
  readonly onCancel: () => void;
}

/**
 * A report field as text, or nothing.
 *
 * The report is composed server-side, but it arrives here as `unknown` like
 * every other response, and `String()` on an object renders the literal
 * `[object Object]` into a merchant's report. An unexpected shape shows as
 * absent instead — wrong is worse than missing on a page somebody forwards to
 * their developer.
 */
function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

/** The merchant-facing half of the report (§5 persona). */
function ReportSummary({ report }: { readonly report: Record<string, unknown> }): React.ReactElement {
  const summary = typeof report["summary"] === "object" && report["summary"] !== null
    ? (report["summary"] as Record<string, unknown>)
    : {};
  const tools = Array.isArray(summary["tools"]) ? summary["tools"] : [];
  const notChecked = Array.isArray(summary["not_checked"]) ? summary["not_checked"] : [];
  const limits = Array.isArray(summary["limits"]) ? summary["limits"] : [];

  return (
    <section className="panel" aria-label="Audit report" id="panel-audit-report" tabIndex={-1}>
      <h3>What we found</h3>
      <p className="audit__headline">
        <strong>{text(summary["headline"])}</strong>
      </p>
      <p>{text(summary["what_this_means"])}</p>

      <ul className="audit__tools">
        {tools.map((entry, index) => {
          const row = typeof entry === "object" && entry !== null ? (entry as Record<string, unknown>) : {};
          return (
            <li key={`${text(row["tool"])}-${String(index)}`}>
              <strong>{text(row["tool"])}</strong> — {text(row["says"])}
              <div className="panel__note">{text(row["what_to_do"])}</div>
            </li>
          );
        })}
      </ul>

      {notChecked.length === 0 ? null : (
        <p>
          <span className="panel__label">Present but not tried:</span>{" "}
          {notChecked.map((name) => text(name)).join(", ")}
        </p>
      )}

      {/* §12.17: a pass is evidence, not a warranty — the report states its own
          limits, and hiding them behind a disclosure would be hiding the part
          that keeps the rest honest. */}
      <details>
        <summary>What this audit does not tell you</summary>
        <ul>
          {limits.map((limit, index) => (
            <li key={String(index)}>{text(limit)}</li>
          ))}
        </ul>
      </details>

      <details>
        <summary>Engineer-grade evidence</summary>
        <pre className="audit__evidence">{JSON.stringify(report["evidence"] ?? [], null, 2)}</pre>
      </details>
    </section>
  );
}

export function AuditSection({
  moduleStatus,
  moduleReason,
  packs,
  audit,
  outcome,
  busy,
  onAuthorize,
  onSubmit,
  onCancel,
}: AuditSectionProps): React.ReactElement {
  const [origin, setOrigin] = useState("");
  const [assertedBy, setAssertedBy] = useState("");
  const [authorized, setAuthorized] = useState(false);
  const [packId, setPackId] = useState("");
  const [transcript, setTranscript] = useState("");
  const originId = useId();
  const actorId = useId();
  const authorizedId = useId();
  const packSelectId = useId();
  const transcriptId = useId();

  const chosen = useMemo(() => packs.find((pack) => pack.packId === packId) ?? null, [packs, packId]);

  if (moduleStatus !== "enabled") {
    // §21.1 and the 009-T12 mechanism: a module that is off says so, with the
    // reason, rather than showing a form that would refuse on submit.
    return (
      <section className="panel" aria-label="External audit" id="panel-audit" tabIndex={-1}>
        <h3>Audit a storefront you did not build</h3>
        <p className="panel__note">
          This deployment has external auditing <strong>{moduleStatus}</strong>
          {moduleReason === "" ? "." : ` — ${moduleReason}`}
        </p>
        <p className="panel__note">
          It is off by default on purpose: an audit needs an origin the operator is authorized on,
          and the allowlist is server-controlled so an anonymous workspace can never point the
          harness at a stranger.
        </p>
      </section>
    );
  }

  return (
    <>
      <section className="panel" aria-label="External audit" id="panel-audit" tabIndex={-1}>
        <h3>Audit a storefront you did not build</h3>
        <p className="panel__note">
          One origin at a time, asserted by a person. The harness never contacts the audited site:
          you run the collector there, in your own session, and bring the transcript back.
        </p>

        {audit === null ? (
          <div className="audit__authorize">
            <label htmlFor={originId}>
              <span className="panel__label">Origin</span>
              <input
                id={originId}
                type="url"
                placeholder="https://your-store.example"
                value={origin}
                disabled={busy}
                onChange={(event) => {
                  setOrigin(event.target.value);
                }}
              />
            </label>
            <label htmlFor={actorId}>
              <span className="panel__label">Asserted by</span>
              <input
                id={actorId}
                type="text"
                placeholder="your name or role"
                value={assertedBy}
                disabled={busy}
                onChange={(event) => {
                  setAssertedBy(event.target.value);
                }}
              />
            </label>
            <label htmlFor={authorizedId} className="audit__affirm">
              <input
                id={authorizedId}
                type="checkbox"
                checked={authorized}
                disabled={busy}
                onChange={(event) => {
                  setAuthorized(event.target.checked);
                }}
              />{" "}
              I am authorized to audit this origin.
            </label>
            <button
              type="button"
              disabled={busy || !authorized || origin === "" || assertedBy === ""}
              onClick={() => {
                onAuthorize(origin, assertedBy);
              }}
            >
              Authorize this audit
            </button>
          </div>
        ) : (
          <div className="audit__live">
            <p>
              <span className="panel__label">Authorized origin:</span> <code>{audit.authorizedOrigin}</code>
            </p>
            <p>
              <span className="panel__label">Asserted by:</span> {audit.assertedBy} —{" "}
              <span className="panel__label">status</span> <strong>{audit.status}</strong>
            </p>
            <button type="button" disabled={busy} onClick={onCancel}>
              Cancel this audit
            </button>
          </div>
        )}
      </section>

      {audit === null ? null : (
        <section className="panel" aria-label="Collect audit evidence" tabIndex={-1}>
          <h3>Collect the evidence</h3>

          <label htmlFor={packSelectId}>
            <span className="panel__label">Contract pack</span>
            <select
              id={packSelectId}
              value={packId}
              disabled={busy}
              onChange={(event) => {
                setPackId(event.target.value);
              }}
            >
              <option value="">Choose a pack…</option>
              {packs.map((pack) => (
                <option key={pack.packId} value={pack.packId}>
                  {pack.title}
                </option>
              ))}
            </select>
          </label>
          <p className="panel__note">
            Packs are offered, never chosen for you (FR-161) — the pack decides whether a write path
            gets exercised against a store somebody depends on.
          </p>

          {chosen === null ? null : (
            <>
              <p className="panel__note">
                Run this on <code>{audit.authorizedOrigin}</code>, then paste what it prints.{" "}
                <strong>Never invoked:</strong> {chosen.neverInvoked.join(", ") || "none"}.
              </p>
              <pre className="audit__collector">{collectorFor(chosen)}</pre>
            </>
          )}

          <label htmlFor={transcriptId}>
            <span className="panel__label">Transcript (JSON)</span>
            <textarea
              id={transcriptId}
              rows={6}
              value={transcript}
              disabled={busy || chosen === null}
              placeholder='{"enumerated": [...], "reports": {...}, "observed_before": {...}, "observed_after": {...}}'
              onChange={(event) => {
                setTranscript(event.target.value);
              }}
            />
          </label>
          <button
            type="button"
            disabled={busy || chosen === null || transcript.trim() === ""}
            onClick={() => {
              onSubmit(packId, transcript);
            }}
          >
            Judge this surface
          </button>
        </section>
      )}

      {outcome === null ? null : <ReportSummary report={outcome.report} />}
    </>
  );
}
