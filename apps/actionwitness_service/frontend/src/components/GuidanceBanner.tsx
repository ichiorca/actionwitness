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

import registry from "../generated/registry.json";

import type { Guidance } from "../api/workspace";

const ACTOR_LABELS: Readonly<Record<string, string>> = {
  operator: "You (operator)",
  agent: "The agent",
  human_approver: "You (approver)",
  harness: "The harness",
};

/**
 * The server's own sentence for each `GuidanceActionCode`, read from the
 * generated registry (AC-6) rather than retyped here.
 *
 * This is the sentence a person reads when a recovery is offered. Until it was
 * looked up, the banner rendered the bare code, so the safe way out of a stalled
 * run was presented to a human being as `reset_workspace` — and §15.1's promise
 * that "the recovery instruction explains why" was met by a token that explains
 * nothing.
 *
 * Reading the registry is not the frontend inventing copy, which FR-120 forbids:
 * `GUIDANCE_ACTION_DESCRIPTIONS` in the core is the single source, and
 * `tests/unit/test_registry.py` fails if this artifact drifts from it. Copy
 * still lives in exactly one place, and §12.13's rule that the code is stable
 * while the sentence changes still holds — the code is what crosses the wire and
 * what history stores.
 */
const ACTION_CODE_COPY: Readonly<Record<string, string>> =
  registry.enums.guidance_action_code.members;

/**
 * The readable sentence for an action code, falling back to the code itself.
 *
 * The fallback is a last resort, not a design: the registry is generated from
 * the same enum the server serializes and a drift test guards it, so a missing
 * key means this build shipped a stale artifact. Showing the raw code then is
 * still better than dropping the recovery, which AC-21 requires the banner to
 * render.
 */
function describeActionCode(code: string): string {
  return ACTION_CODE_COPY[code] ?? code;
}

export interface GuidanceBannerProps {
  readonly guidance: Guidance | null;
  readonly loading: boolean;
  /**
   * The `id` of the control that performs the server's named action, when this
   * page has one. `null`/absent renders no shortcut — some actions have no
   * human control here (an agent's turn, a modal that presents itself), and a
   * missing mapping must degrade to the banner exactly as it was.
   */
  readonly actionTargetId?: string | null;
  /**
   * Overrides the default walk when the owner must do something first — the
   * workspace passes this to bring the right view forward before focusing,
   * since a control on a hidden view cannot receive the reader.
   */
  readonly onGo?: (targetId: string) => void;
}

/**
 * Bring the reader to the control the instruction names (FR-120-compatible:
 * the *server* chose the action; this only closes the distance to the control
 * that performs it, and invents nothing when the element is not on the page).
 *
 * The flash class is removed on `animationend` rather than a timer, so nothing
 * here depends on wall-clock time; in an environment that never animates the
 * class is inert. Smooth scrolling defers to `prefers-reduced-motion`.
 */
export function goToAction(targetId: string): void {
  const target = document.getElementById(targetId);
  if (target === null) {
    return;
  }
  if (typeof target.scrollIntoView === "function") {
    const reduced =
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
  }
  target.focus({ preventScroll: true });
  target.classList.add("target-flash");
  target.addEventListener(
    "animationend",
    () => {
      target.classList.remove("target-flash");
    },
    { once: true },
  );
}

export function GuidanceBanner({
  guidance,
  loading,
  actionTargetId,
  onGo,
}: GuidanceBannerProps): React.ReactElement {
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
          {actionTargetId == null ? null : (
            // The instruction in words stays the guidance; this only walks the
            // reader to the control that performs it, several panels down.
            <button
              type="button"
              className="banner__go"
              onClick={() => {
                (onGo ?? goToAction)(actionTargetId);
              }}
            >
              Go to this step
            </button>
          )}
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
          {/* The sentence, not the token. The code is still exposed below for
              the surfaces AC-21 requires to agree on it. */}
          <span className="banner__label">If this stalls:</span>{" "}
          {describeActionCode(guidance.recoveryActionCode)}
          <span hidden data-testid="banner-recovery-action-code">
            {guidance.recoveryActionCode}
          </span>
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
