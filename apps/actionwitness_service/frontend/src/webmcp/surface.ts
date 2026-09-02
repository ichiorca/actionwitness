/**
 * Capturing the browser's tool surface for the server to judge (FR-166, FR-167).
 *
 * This is the only place `getTools()` can be read from, so it is the only place
 * a capture can begin — and it deliberately does almost nothing with what it
 * reads. Definitions go to the server exactly as the browser reported them. No
 * hash is computed here and no namespace is assigned here, because a page that
 * could do either would be the tool surface vouching for its own integrity,
 * which is the category error this whole feature exists to catch.
 *
 * Three behaviours follow from FR-167 and the 006 lifecycle discipline:
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
 * **A stale response is dropped, never applied.** StrictMode mounts twice and a
 * run can end mid-flight; a capture that resolved after teardown belongs to a
 * page that has gone.
 */

import { useEffect } from "react";

import { request } from "../api/client";

/** How long a `toolchange` burst is allowed to settle before re-reading. */
export const TOOLCHANGE_QUIET_PERIOD_MS = 150;

/**
 * One tool, in the shape the capture route accepts.
 *
 * No `identity_hash` and no `namespace`: the server computes both. Adding
 * either here would not merely be redundant — it would move a decision the
 * server must own onto the least trustworthy side of the boundary.
 */
export interface CapturedTool {
  readonly name: string;
  readonly description: string;
  readonly read_only_hint: boolean | null;
  readonly untrusted_content_hint: boolean | null;
  readonly input_schema: Record<string, unknown>;
}

/**
 * Narrow one descriptor from `getTools()`.
 *
 * Everything arrives as `unknown`: these objects come from the browser's tool
 * registry, which any script on the origin can write to. A descriptor missing a
 * usable name is dropped rather than defaulted — a tool the server cannot name
 * is one it cannot compare against a baseline, and inventing a name would
 * invent a delta.
 */
export function describeTool(value: unknown): CapturedTool | null {
  if (typeof value !== "object" || value === null) {
    return null;
  }
  const record = value as Record<string, unknown>;
  const name = record["name"];
  if (typeof name !== "string" || name.length === 0) {
    return null;
  }
  return {
    name,
    description: typeof record["description"] === "string" ? record["description"] : "",
    // Absent stays absent. A tool that stopped *declaring* itself read-only
    // changed its hints, and coercing the absence to `false` would hide that.
    read_only_hint: typeof record["readOnlyHint"] === "boolean" ? record["readOnlyHint"] : null,
    untrusted_content_hint:
      typeof record["untrustedContentHint"] === "boolean"
        ? record["untrustedContentHint"]
        : null,
    input_schema: isRecord(record["inputSchema"]) ? record["inputSchema"] : {},
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Read the whole surface, or `null` when this browser has no WebMCP. */
export async function readSurface(): Promise<readonly CapturedTool[] | null> {
  const modelContext = document.modelContext;
  if (modelContext === undefined) {
    return null;
  }
  const tools = await modelContext.getTools();
  return tools
    .map((tool) => describeTool(tool))
    .filter((tool): tool is CapturedTool => tool !== null);
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
export function useToolSurfaceWitness(runId: string | null): void {
  useEffect(() => {
    if (runId === null) {
      return;
    }
    const modelContext = document.modelContext;
    if (modelContext === undefined) {
      return;
    }

    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    // Scoped to this effect, deliberately not a ref. A ref survives StrictMode's
    // unmount/remount, so the second mount would find the first mount's capture
    // still in flight and skip — while that first capture discards itself on
    // `live`, leaving the run with no baseline at all. Per-effect state makes
    // the stale capture the only one dropped.
    let inFlight = false;

    const capture = async (): Promise<void> => {
      // One capture at a time. Overlapping posts would append events whose
      // order does not match the order the surfaces were read in, and the
      // baseline is whichever landed first.
      if (inFlight) {
        return;
      }
      inFlight = true;
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
      } finally {
        inFlight = false;
      }
    };

    const onToolChange = (): void => {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
      // Coalesces a burst into one read, never a change into silence: the read
      // that follows sees the settled surface, which is the one that matters.
      timer = setTimeout(() => void capture(), TOOLCHANGE_QUIET_PERIOD_MS);
    };

    modelContext.addEventListener("toolchange", onToolChange);
    void capture();

    return () => {
      live = false;
      if (timer !== undefined) {
        clearTimeout(timer);
      }
      modelContext.removeEventListener("toolchange", onToolChange);
    };
  }, [runId]);
}
