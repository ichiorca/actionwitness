/**
 * Authoritative workspace state, loaded from FastAPI (§15.1, FR-120).
 *
 * The rule this exists to enforce: **the server decides, the browser displays.**
 * Phase, next action, and every control's enablement come from `GET /workspace`;
 * nothing here computes a phase from a run status. Two derivations would agree
 * in testing and diverge exactly when a person and an agent disagree about whose
 * turn it is, which is the situation guidance exists for.
 *
 * `refresh` is what every mutation calls after it succeeds. That is deliberately
 * a re-read rather than a local edit: a component that patched its own copy
 * would be inventing the state the server is authoritative for, and would be
 * wrong whenever the server did something the client did not predict — which,
 * for a harness whose whole subject is unexpected outcomes, is often.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, request } from "../api/client";
import { type WorkspaceStatus, parseWorkspace } from "../api/workspace";

export interface WorkspaceState {
  readonly status: WorkspaceStatus | null;
  readonly error: string | null;
  readonly loading: boolean;
  readonly refresh: () => Promise<void>;
}

export function useWorkspace(): WorkspaceState {
  const [status, setStatus] = useState<WorkspaceStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  // Every in-flight load, so a newer one can cancel an older. Without this a
  // slow first response can land after a fast second and put stale state on
  // screen — the classic polling bug, and one a user only sees intermittently.
  const inFlight = useRef<AbortController | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      inFlight.current?.abort();
    };
  }, []);

  const refresh = useCallback(async () => {
    inFlight.current?.abort();
    const controller = new AbortController();
    inFlight.current = controller;

    try {
      const next = await request("/workspace", {
        parse: parseWorkspace,
        signal: controller.signal,
      });
      if (mounted.current && !controller.signal.aborted) {
        setStatus(next);
        setError(null);
      }
    } catch (caught: unknown) {
      if (caught instanceof DOMException && caught.name === "AbortError") {
        return; // Superseded by a newer load, or unmounted. Not a failure.
      }
      if (mounted.current) {
        setError(caught instanceof ApiError ? caught.message : "The harness could not be reached.");
      }
    } finally {
      if (mounted.current && !controller.signal.aborted) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { status, error, loading, refresh };
}
