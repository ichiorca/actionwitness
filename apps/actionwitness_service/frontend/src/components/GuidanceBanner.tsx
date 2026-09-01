/**
 * The guidance banner (§14, FR-120, FR-121, AC-21).
 *
 * Rendered above the working panels and derived from nothing. Every field comes
 * from the server's `GuidanceState`; this component chooses no phase, computes
 * no next action, and has no fallback copy of its own. FR-120 is explicit that
 * "the frontend shall not invent a conflicting next action", and the way that
 * rule gets broken is not by ignoring it — it is by adding a sensible-looking
 * default for a case the server had not covered yet.
 *
 * AC-21 asks a blocking transition to name **one** active actor, **one** primary
 * next action, why it is required, its expected consequence, any waiting
 * condition, and a safe recovery. All six are rendered, and the absence of a
 * primary action is rendered as an absence: §15.1 says "if no safe action is
 * possible, the primary action is omitted and the recovery instruction explains
 * why", so an empty action code shows the recovery rather than a blank space
 * that reads as a loading state.
 *
 * `aria-live="polite"` because control moving between a person and an agent is
 * exactly the change a screen-reader user must not have to go looking for.
 */

import type { Guidance } from "../api/workspace";

const ACTOR_LABELS: Readonly<Record<string, string>> = {
  operator: "You (operator)",
  agent: "The agent",
  human_approver: "You (approver)",
  harness: "The harness",
};

export interface GuidanceBannerProps {
  readonly guidance: Guidance | null;
  readonly loading: boolean;
}

export function GuidanceBanner({ guidance, loading }: GuidanceBannerProps): React.ReactElement {
  if (guidance === null) {
    return (
      <section className="banner" aria-live="polite" aria-busy={loading}>
        <p>{loading ? "Loading the workspace…" : "The workspace could not be read."}</p>
      </section>
    );
  }

  const actor = ACTOR_LABELS[guidance.activeActor] ?? guidance.activeActor;

  return (
    <section className="banner" aria-live="polite" data-phase={guidance.phase}>
      <h2 className="banner__headline">{guidance.headline}</h2>
      <p className="banner__actor">
        {/* Exactly one active actor, named in words rather than by colour or
            position — AC-21's guidance must be understandable without either. */}
        <span className="banner__label">Whose turn:</span> {actor}
      </p>
      {guidance.actionCode === null ? (
        <p className="banner__recovery">
          <span className="banner__label">No safe action right now.</span> {guidance.instruction}
        </p>
      ) : (
        <p className="banner__action">
          <span className="banner__label">Next:</span> {guidance.instruction}
        </p>
      )}
      <p className="banner__reason">
        <span className="banner__label">Why:</span> {guidance.reason}
      </p>
      <p className="banner__consequence">
        <span className="banner__label">What happens:</span> {guidance.expectedConsequence}
      </p>
      {guidance.waitingFor === null ? null : (
        <p className="banner__waiting">
          <span className="banner__label">Waiting for:</span> {guidance.waitingFor}
        </p>
      )}
      {guidance.recoveryActionCode === null ? null : (
        <p className="banner__recovery-code">
          <span className="banner__label">If this stalls:</span> {guidance.recoveryActionCode}
        </p>
      )}
      {/* The action code is what AC-21 requires the banner, the tool result,
          the enabled controls, and the history to agree on. Exposed as data
          rather than prose so a test can compare it against the others. */}
      <span hidden data-testid="banner-action-code">
        {guidance.actionCode ?? ""}
      </span>
    </section>
  );
}
