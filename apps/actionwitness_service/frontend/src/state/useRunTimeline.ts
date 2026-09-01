/**
 * Paged polling of a run's timeline (§15.3, 006-T10).
 *
 * §15.3 makes polling pagination normative for Tier 1, and the cursor is the
 * whole mechanism: sequence numbers are dense and monotonic per run, so
 * "everything after N" is a position this page can hold across reconnects
 * without the server remembering anything about it.
 *
 * Three properties, each of which is a bug people ship without noticing:
 *
 * - **`has_more: false` does not mean the run ended.** It means nothing more
 *   exists *right now*. Polling stops on a terminal `run_status`, never on an
 *   empty page — a client that stopped on the empty page would miss the events
 *   a failing run is judged by.
 * - **A stale response is dropped, not rendered.** A slow page can land after a
 *   fast one; applying it would rewind the timeline in front of the user.
 * - **Unmounting cancels.** An in-flight poll whose component has gone must not
 *   set state, and must not keep the connection alive on a page nobody sees.
 *
 * The interval is a plain timeout chained after each completed poll rather than
 * a fixed `setInterval`, so a slow response cannot stack requests on top of
 * each other.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { request } from "../api/client";
import { type EventPage, type TimelineEvent, parseEventPage } from "../api/workspace";

const POLL_INTERVAL_MS = 1_000;
const PAGE_SIZE = 100;

const TERMINAL_RUN_STATES = [
  "passed",
  "passed_with_warnings",
  "failed",
  "error",
  "cancelled",
];

export interface RunTimelineState {
  readonly events: readonly TimelineEvent[];
  readonly runStatus: string | null;
  readonly polling: boolean;
  readonly error: string | null;
}

export function useRunTimeline(runId: string | null, intervalMs = POLL_INTERVAL_MS): RunTimelineState {
  const [events, setEvents] = useState<readonly TimelineEvent[]>([]);
  const [runStatus, setRunStatus] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [polling, setPolling] = useState(false);

  // The cursor lives in a ref rather than state: advancing it must not trigger
  // a render, and a render must not reset it.
  const cursor = useRef(0);

  useEffect(() => {
    cursor.current = 0;
    setEvents([]);
    setRunStatus(null);
    setError(null);
  }, [runId]);

  const pollOnce = useCallback(
    async (signal: AbortSignal): Promise<EventPage | null> => {
      if (runId === null) {
        return null;
      }
      return await request(
        `/runs/${runId}/events?after_sequence=${String(cursor.current)}&limit=${String(PAGE_SIZE)}`,
        { parse: parseEventPage, signal },
      );
    },
    [runId],
  );

  useEffect(() => {
    if (runId === null) {
      setPolling(false);
      return;
    }

    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    let live = true;
    setPolling(true);

    const tick = async (): Promise<void> => {
      try {
        const page = await pollOnce(controller.signal);
        // The component may have gone while this was in flight. Writing state
        // here would be a leak and, worse, could resurrect a stale run.
        if (!live || page === null) {
          return;
        }

        if (page.events.length > 0) {
          cursor.current = page.nextAfterSequence;
          setEvents((previous) => [...previous, ...page.events]);
        }
        setRunStatus(page.runStatus);
        setError(null);

        // Stop on the run's own status, never on an empty page: `has_more:
        // false` means "nothing more right now", and a live run keeps
        // appending.
        if (TERMINAL_RUN_STATES.includes(page.runStatus)) {
          setPolling(false);
          return;
        }
      } catch (caught: unknown) {
        if (caught instanceof DOMException && caught.name === "AbortError") {
          return;
        }
        if (live) {
          setError("The timeline could not be read.");
        }
      }

      if (live) {
        // Chained rather than an interval, so a slow response cannot stack
        // requests behind it.
        timer = setTimeout(() => void tick(), intervalMs);
      }
    };

    void tick();

    return () => {
      live = false;
      controller.abort();
      if (timer !== null) {
        clearTimeout(timer);
      }
    };
  }, [runId, pollOnce, intervalMs]);

  return { events, runStatus, polling, error };
}
