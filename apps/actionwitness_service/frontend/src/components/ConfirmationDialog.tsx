/**
 * The human confirmation dialog (§14.2, §14.4, §14.14, AC-06, AC-21).
 *
 * ## Nothing is preselected
 *
 * §14.4: "no option is preselected and the agent cannot choose either control."
 * That reads like an accessibility detail and is a safety property: a focused
 * default *Approve* is a consent flow that a stray Enter key completes. Focus
 * therefore lands on the dialog itself, and a person has to reach a button
 * deliberately.
 *
 * ## Focus is trapped and restored
 *
 * A modal a keyboard user can tab out of is a modal they can lose, leaving them
 * operating a page that is waiting on them. On close, focus returns to whatever
 * had it before — otherwise the caret jumps to the top of the document and the
 * reader has to find their place again.
 *
 * ## Every status has a text alternative
 *
 * The expiry, the cart, and the outcome are all rendered as words. A dialog
 * that showed a countdown ring and a green tick would be undecidable for
 * someone who could see neither.
 *
 * ## The other tab
 *
 * §14.14: a second tab on the same workspace shows a read-only "pending in
 * another tab" banner and offers no decision controls. It is the same
 * confirmation and the same authority; what it lacks is the pending promise,
 * so a decision made here would resolve nothing there.
 */

import { useEffect, useRef } from "react";

import type { PendingConfirmation } from "../api/workspace";

export interface ConfirmationDialogProps {
  readonly pending: PendingConfirmation;
  /** Whether *this* tab owns the waiting invocation (§14.14). */
  readonly owned: boolean;
  readonly busy: boolean;
  readonly onApprove: () => void;
  readonly onDeny: () => void;
}

export function ConfirmationDialog({
  pending,
  owned,
  busy,
  onApprove,
  onDeny,
}: ConfirmationDialogProps): React.ReactElement {
  const dialog = useRef<HTMLDivElement | null>(null);
  const restoreTo = useRef<Element | null>(null);

  useEffect(() => {
    restoreTo.current = document.activeElement;
    // Focus the dialog, not a button: §14.4 forbids a preselected choice, and
    // focusing "Approve" is a preselection whatever the styling says.
    dialog.current?.focus();

    return () => {
      // Restoring focus is what stops a keyboard user being dropped at the top
      // of the document with no idea where the dialog went.
      if (restoreTo.current instanceof HTMLElement) {
        restoreTo.current.focus();
      }
    };
  }, []);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent): void {
      if (event.key !== "Tab" || dialog.current === null) {
        return;
      }
      const focusable = dialog.current.querySelectorAll<HTMLElement>(
        "button:not([disabled]), [href], [tabindex]:not([tabindex='-1'])",
      );
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (first === undefined || last === undefined) {
        return;
      }
      // Wrap at both ends. A trap that only caught forward Tab lets
      // Shift+Tab walk out of the modal, which is the same bug facing the
      // other way.
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }

    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
    };
  }, []);

  const consequence = pending.consequence;

  return (
    <div
      className="dialog"
      role="dialog"
      aria-modal="true"
      aria-labelledby="confirmation-title"
      aria-describedby="confirmation-detail"
      tabIndex={-1}
      ref={dialog}
    >
      <h2 id="confirmation-title">Approve this action?</h2>

      <div id="confirmation-detail">
        <p>
          <span className="dialog__label">The agent wants to:</span>{" "}
          <strong>{pending.toolName}</strong>
        </p>
        <p>
          {/* Text, not a countdown ring: an expiry nobody can read is an
              expiry that will surprise them. */}
          <span className="dialog__label">Expires at:</span> {pending.expiresAt}
        </p>
        <p>
          <span className="dialog__label">What it affects:</span>
        </p>
        <pre className="dialog__consequence">{JSON.stringify(consequence, null, 2)}</pre>
        <p className="dialog__note">
          Nothing has changed yet. Approving performs this action once; denying leaves everything
          as it is.
        </p>
      </div>

      {owned ? (
        <div className="dialog__choices">
          {/* Neither is `autoFocus`, and neither is a form's default submit —
              §14.4's "no option is preselected", enforced rather than styled. */}
          <button type="button" onClick={onApprove} disabled={busy}>
            Approve once
          </button>
          <button type="button" onClick={onDeny} disabled={busy}>
            Deny
          </button>
        </div>
      ) : (
        <p role="status" className="dialog__elsewhere">
          This decision is pending in another tab. Answer it there — this tab is not the one the
          agent is waiting on.
        </p>
      )}

      <p aria-live="polite" className="dialog__status">
        {busy ? "Recording your decision…" : "Waiting for your decision."}
      </p>
    </div>
  );
}
