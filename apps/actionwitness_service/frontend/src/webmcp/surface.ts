/**
 * Capturing the browser's tool surface for the server to judge (FR-166, FR-167).
 *
 * The surface itself is read through the adapter's `readSurface`, which is the
 * product's only `getTools()` call (012-T6). That matters here specifically:
 * the registration view a person reads and the capture the server judges must
 * come from the same read, or the page could show "all registered" while the
 * evidence recorded something else.
 *
 * This module deliberately does almost nothing with what it reads. Definitions
 * go to the server exactly as the browser reported them. No hash is computed
 * here and no namespace is assigned here, because a page that could do either
 * would be the tool surface vouching for its own integrity, which is the
 * category error this whole feature exists to catch.
 *
 * Four behaviours follow from FR-167 and the 006 lifecycle discipline:
 *
 * **`getTools()` is the authority.** Reconciliation never infers the surface
 * from component mount state — FR-167 says so explicitly, and the two genuinely
 * disagree: another script on the origin can register tools this app never
 * mounted, which is precisely the attack the policy watches for.
 *
 * **Every firing re-captures.** The debounce below coalesces a *burst* into one
 * read, not a change into silence: ADR-0002 recorded that `toolchange` fires per
 * change and does not coalesce, so a busy registration sequence would otherwise
 * post a dozen near-identical captures. The server records every capture it
 * receives, so a quiet re-capture still proves the surface was looked at.
 *
 * **A change during a capture is deferred, never dropped.** One capture is in
 * flight at a time — overlapping posts would append events in an order that does
 * not match the order the surfaces were read in — but a `toolchange` that
 * arrives while one is posting schedules the next read instead of being
 * discarded. Discarding it is how a mid-run injection escaped the record
 * entirely: the demo's own look-alike registers in the same commit that arms the
 * run, so its `toolchange` lands squarely inside the baseline's own POST.
 *
 * **A stale response is dropped, never applied.** StrictMode mounts twice and a
 * run can end mid-flight; a capture that resolved after teardown belongs to a
 * page that has gone.
 */

import { useCallback, useEffect, useRef } from "react";

import { request } from "../api/client";
import { readSurface, subscribeToToolChange } from "./adapter";

/** How long a `toolchange` burst is allowed to settle before re-reading. */
export const TOOLCHANGE_QUIET_PERIOD_MS = 150;

/**
 * Rounds one capture may spend catching up with a registry that keeps moving.
 *
 * Two is what the case this exists for needs: a change that landed during one
 * post is caught by the next. A bound rather than a loop, because a page that
 * re-registered on every read would otherwise spin here forever.
 */
const MAX_CATCH_UP_ROUNDS = 2;

/**
 * Iterations one `flush` may spend waiting for the witness to go quiet.
 *
 * Each iteration either awaits a capture already in flight or starts the one
 * that is owed, so the bound is on how many times those can alternate — not on
 * how many requests are made.
 */
const FLUSH_ROUNDS = 4;

export interface ToolSurfaceWitness {
  /**
   * Put any outstanding capture on the record, and wait for it.
   *
   * Verification seals the run's timeline, and the witness is asynchronous and
   * debounced — so without this a delta observed a moment before `verify_outcome`
   * is posted a moment after, meets a sealed timeline, and never reaches the
   * verdict it was evidence for. Every path that verifies awaits this first.
   *
   * Safe to call with nothing pending, with no run, and in a browser with no
   * WebMCP: each resolves immediately, because "there is no evidence to flush"
   * and "the evidence is flushed" are the same postcondition.
   */
  readonly flush: () => Promise<void>;
}

/**
 * Capture at arming and on every `toolchange` for the life of `runId`.
 *
 * Does nothing without a run: a capture belongs to a run's timeline, and
 * posting one before arming would be evidence about nothing. Does nothing
 * without WebMCP either, which leaves the run with no baseline — and §16.1
 * requires `stable_tool_surface` to fail closed on that rather than pass, so
 * silence here is a refusal there rather than a gap.
 */
export function useToolSurfaceWitness(runId: string | null): ToolSurfaceWitness {
  // The live effect's flusher. A ref rather than state because replacing it must
  // not render, and because the caller holds one stable `flush` across mounts.
  const pending = useRef<(() => Promise<void>) | null>(null);

  useEffect(() => {
    if (runId === null) {
      return;
    }
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Scoped to this effect, deliberately not a ref. A ref survives StrictMode's
    // unmount/remount, so the second mount would find the first mount's capture
    // still in flight and skip — while that first capture discards itself on
    // `live`, leaving the run with no baseline at all. Per-effect state makes
    // the stale capture the only one dropped.
    let inFlight: Promise<void> | null = null;
    // A change that arrived while a capture was posting. Held rather than
    // dropped: the surface it describes is the one the policy is about to judge.
    let missed = false;

    const post = async (): Promise<void> => {
      try {
        const tools = await readSurface();
        if (!live || tools === null) {
          return;
        }
        await request(`/runs/${runId}/tool-surface`, {
          method: "POST",
          body: { tools },
          // The response is not read. What matters is that the capture was
          // recorded, and the server's own tests hold the shape it returns.
          parse: () => undefined,
        });
      } catch {
        // A failed capture is not a failed run. The absence of a baseline is
        // already an explicit non-pass at verification (§16.1); throwing here
        // would take down the workspace UI for an evidence-collection problem.
      }
    };

    /** Post one surface, then post again if the surface moved while it did. */
    const capture = async (): Promise<void> => {
      // One capture at a time. Overlapping posts would append events whose
      // order does not match the order the surfaces were read in, and the
      // baseline is whichever landed first.
      if (inFlight !== null) {
        missed = true;
        return await inFlight;
      }
      let round = 0;
      do {
        missed = false;
        inFlight = post();
        await inFlight;
        inFlight = null;
        round += 1;
      } while (missed && live && round < MAX_CATCH_UP_ROUNDS);
    };

    const onToolChange = (): void => {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
      // Coalesces a burst into one read, never a change into silence: the read
      // that follows sees the settled surface, which is the one that matters.
      timer = setTimeout(() => void capture(), TOOLCHANGE_QUIET_PERIOD_MS);
    };

    // `null` means this browser has no WebMCP, so there is nothing to watch and
    // nothing to capture. The run is then left with no baseline — which §16.1
    // turns into an explicit non-pass rather than a silent gap.
    const unsubscribe = subscribeToToolChange(onToolChange);
    if (unsubscribe === null) {
      return;
    }
    void capture();

    pending.current = async (): Promise<void> => {
      // Any debounced read is owed to this run now, rather than after a quiet
      // period the caller is about to make irrelevant.
      if (timer !== undefined) {
        clearTimeout(timer);
        timer = undefined;
        missed = true;
      }
      // Drain rather than capture: a flush with nothing outstanding must cost
      // nothing. It runs before every verification, and a request per verdict
      // spent re-reading an unchanged surface is a request the page's own
      // polling does not get to make — FR-009's budget is shared.
      //
      // The loop is what makes "outstanding" reliable. A capture already in
      // flight may itself re-run when it finds a change arrived during its
      // post, so awaiting one promise is not the same as waiting for quiet.
      for (let round = 0; round < FLUSH_ROUNDS; round += 1) {
        const running = inFlight;
        if (running !== null) {
          await running;
          continue;
        }
        if (!missed) {
          return;
        }
        await capture();
      }
    };

    return () => {
      live = false;
      pending.current = null;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
      unsubscribe();
    };
  }, [runId]);

  const flush = useCallback(async (): Promise<void> => {
    await pending.current?.();
  }, []);

  return { flush };
}
