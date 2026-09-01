/**
 * The in-page correlation map §14.14 describes.
 *
 * "The invoking page owns the pending `execute` promise. Its confirmation
 * component posts the decision and signals the waiting handler through an
 * in-page correlation map keyed by `confirmation_id`."
 *
 * That sentence is a design, and this is it. A tool handler that has paused for
 * consent parks a promise here; the dialog resolves it once the server has
 * answered. **No server transaction is held open across the wait** — the
 * confirmation row is already committed, and this map holds nothing but a
 * callback in one browser tab.
 *
 * Three things it must get right, each of which is a way for a person to end up
 * staring at a dialog nobody is waiting on:
 *
 * - **A cancelled invocation settles its waiter.** If an agent abandons the
 *   call, the handler's signal fires and the waiter must be released — with a
 *   cancellation, so the page can tell the server too (§14.9).
 * - **Every waiter settles exactly once.** A decision arriving after a
 *   cancellation must not resolve a promise that already rejected.
 * - **A waiter that nobody claims is dropped, not leaked.** A tab that reloads
 *   loses its map; the server's expiry is what covers that case, which is why
 *   this holds no authority of its own.
 */

export type ConfirmationOutcome =
  | { readonly kind: "approved" }
  | { readonly kind: "refused"; readonly status: string; readonly detail: string }
  | { readonly kind: "cancelled" };

interface Waiter {
  readonly settle: (outcome: ConfirmationOutcome) => void;
}

/**
 * One tab's pending confirmations.
 *
 * A class rather than module state so a test can build an isolated one, and so
 * two tabs never share anything by construction — they are separate documents
 * and separate maps, which is exactly what §14.14's cross-tab rule assumes.
 */
export class ConfirmationCoordinator {
  readonly #waiters = new Map<string, Waiter>();
  readonly #listeners = new Set<() => void>();

  /**
   * Park until this confirmation is decided.
   *
   * `signal` is the invocation's own. When it aborts — the agent walked away —
   * the wait ends as `cancelled` rather than hanging, and the caller is
   * expected to tell the server so the human's dialog closes too.
   */
  async wait(
    confirmationId: string,
    signal: AbortSignal | undefined,
  ): Promise<ConfirmationOutcome> {
    // No signal is the pinned build's normal case (ADR-0002: executeTool
    // forwards none). The wait still resolves through settle(); it simply
    // cannot be caller-cancelled, which is the documented degradation.
    return await new Promise<ConfirmationOutcome>((resolve) => {
      let settled = false;
      const settle = (outcome: ConfirmationOutcome): void => {
        // Guarded because a decision and a cancellation can race: whichever
        // arrives first is the outcome, and the second must not overwrite it.
        if (settled) {
          return;
        }
        settled = true;
        this.#waiters.delete(confirmationId);
        signal?.removeEventListener("abort", onAbort);
        this.#announce();
        resolve(outcome);
      };

      function onAbort(): void {
        settle({ kind: "cancelled" });
      }

      if (signal?.aborted) {
        settle({ kind: "cancelled" });
        return;
      }

      this.#waiters.set(confirmationId, { settle });
      signal?.addEventListener("abort", onAbort, { once: true });
      this.#announce();
    });
  }

  /** Release a waiter with the decision the server recorded. */
  settle(confirmationId: string, outcome: ConfirmationOutcome): void {
    this.#waiters.get(confirmationId)?.settle(outcome);
  }

  /** Whether this tab is the one waiting on `confirmationId` (§14.14). */
  isWaiting(confirmationId: string): boolean {
    return this.#waiters.has(confirmationId);
  }

  get pendingIds(): readonly string[] {
    return [...this.#waiters.keys()];
  }

  subscribe(listener: () => void): () => void {
    this.#listeners.add(listener);
    return () => this.#listeners.delete(listener);
  }

  #announce(): void {
    for (const listener of this.#listeners) {
      listener();
    }
  }
}

/** The coordinator for this document. One page, one map. */
export const confirmations = new ConfirmationCoordinator();
