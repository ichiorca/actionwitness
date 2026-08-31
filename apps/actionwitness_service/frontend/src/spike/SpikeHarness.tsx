/**
 * ADR-0002 spike harness (spec §25.1, §33 open question 2).
 *
 * A human runs this against the exact target Chrome/ChatGPT build and compares
 * three registration paths for one read-only tool. It answers two questions the
 * decision turns on:
 *
 *   1. Does the candidate register and clean up exactly once under StrictMode?
 *   2. Does it forward the per-invocation `{ signal }`? A hook that does not
 *      cannot carry `proceed_to_checkout` (FR-037, LD-4).
 *
 * The tool count comes from `document.modelContext.getTools()`, not from this
 * component's own state — the browser is the authority on what is registered,
 * and reconciling from component state is precisely the bug this looks for.
 */

import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";

import { HOOK_CANDIDATES, type HookCandidate, HookProbe, useHookCandidate } from "./hookPath";
import { isWebMcpAvailable, listRegisteredTools, useNativeToolRegistration } from "./nativePath";
import { SPIKE_TOOL_NAME, createSpikeTool } from "./tool";

type PathId = "native" | HookCandidate;
const PATHS: readonly PathId[] = ["native", ...HOOK_CANDIDATES];

interface LogLine {
  readonly seq: number;
  readonly text: string;
}

function useLog(): [LogLine[], (text: string) => void, () => void] {
  const [lines, setLines] = useState<LogLine[]>([]);
  const append = useCallback((text: string) => {
    setLines((current) => [...current, { seq: current.length + 1, text }]);
  }, []);
  const clear = useCallback(() => setLines([]), []);
  return [lines, append, clear];
}

/** Poll the browser's own view of the registry. Never trust component state here. */
function useRegisteredTools(refreshToken: number): WebMCP.RegisteredTool[] {
  const [tools, setTools] = useState<WebMCP.RegisteredTool[]>([]);

  useEffect(() => {
    let cancelled = false;
    const refresh = (): void => {
      void listRegisteredTools().then((next) => {
        if (!cancelled) {
          setTools(next);
        }
      });
    };
    refresh();

    const modelContext = document.modelContext;
    modelContext?.addEventListener("toolchange", refresh);
    return () => {
      cancelled = true;
      modelContext?.removeEventListener("toolchange", refresh);
    };
  }, [refreshToken]);

  return tools;
}

function NativePath({
  tool,
  onLog,
}: {
  tool: WebMCP.ModelContextTool;
  onLog: (text: string) => void;
}): ReactNode {
  const state = useNativeToolRegistration(tool);

  useEffect(() => {
    onLog(`native: ${state.phase} — ${state.detail}`);
  }, [state, onLog]);

  return <p>Native registration: <strong>{state.phase}</strong> — {state.detail}</p>;
}

function HookPath({
  specifier,
  tool,
  onLog,
}: {
  specifier: HookCandidate;
  tool: WebMCP.ModelContextTool;
  onLog: (text: string) => void;
}): ReactNode {
  const load = useHookCandidate(specifier);

  useEffect(() => {
    onLog(`${specifier}: ${load.status}${"detail" in load ? ` — ${load.detail}` : ""}`);
  }, [specifier, load, onLog]);

  if (load.status === "loading") {
    return <p>Loading {specifier}…</p>;
  }
  if (load.status === "missing") {
    return (
      <div>
        <p role="status">
          <strong>{specifier} is not installed.</strong>
        </p>
        <pre>npm install {specifier}</pre>
        <p>
          Install exactly one candidate at a time, reload, and record the result in
          ADR-0002. Do not commit the resulting lockfile until the pin is decided.
        </p>
      </div>
    );
  }
  if (load.status === "unexpected") {
    return (
      <div>
        <p role="alert">{load.detail}</p>
        <p>Exports seen: {load.exports.join(", ") || "(none)"}</p>
      </div>
    );
  }

  return (
    <div>
      <p>
        Loaded <code>{specifier}</code> via export <code>{load.exportName}</code>.
      </p>
      {/* key remounts on candidate change, which keeps the rules of hooks intact */}
      <HookProbe
        key={specifier}
        hook={load.hook}
        tool={tool}
        onError={(message) => onLog(`${specifier}: threw — ${message}`)}
      />
    </div>
  );
}

export default function SpikeHarness(): ReactNode {
  const [path, setPath] = useState<PathId>("native");
  const [mounted, setMounted] = useState(true);
  const [refreshToken, setRefreshToken] = useState(0);
  const [lines, append, clear] = useLog();
  const [invocations, setInvocations] = useState<
    { signalPresent: boolean; aborted: boolean }[]
  >([]);

  const tool = useMemo(
    () => createSpikeTool((report) => setInvocations((current) => [...current, report])),
    [],
  );

  const registered = useRegisteredTools(refreshToken);
  const spikeToolCount = registered.filter((entry) => entry.name === SPIKE_TOOL_NAME).length;

  return (
    <main>
      <h1>WebMCP lifecycle spike (ADR-0002)</h1>

      <section aria-labelledby="env">
        <h2 id="env">Environment</h2>
        <p>
          WebMCP support:{" "}
          <strong>{isWebMcpAvailable() ? "available" : "absent"}</strong>
        </p>
        <p>
          Record the exact browser build and flag configuration in the ADR — type
          availability never proves runtime support.
        </p>
      </section>

      <section aria-labelledby="path">
        <h2 id="path">Registration path</h2>
        {PATHS.map((candidate) => (
          <label key={candidate}>
            <input
              type="radio"
              name="path"
              value={candidate}
              checked={path === candidate}
              onChange={() => {
                setPath(candidate);
                append(`--- switched to ${candidate} ---`);
              }}
            />
            {candidate}
          </label>
        ))}
      </section>

      <section aria-labelledby="lifecycle">
        <h2 id="lifecycle">Lifecycle</h2>
        <button type="button" onClick={() => setMounted((current) => !current)}>
          {mounted ? "Unmount" : "Mount"} the registering component
        </button>
        <button type="button" onClick={() => setRefreshToken((n) => n + 1)}>
          Re-read getTools()
        </button>
        <button type="button" onClick={clear}>
          Clear log
        </button>

        {mounted ? (
          path === "native" ? (
            <NativePath tool={tool} onLog={append} />
          ) : (
            <HookPath specifier={path} tool={tool} onLog={append} />
          )
        ) : (
          <p>Unmounted. getTools() must now report zero {SPIKE_TOOL_NAME} entries.</p>
        )}
      </section>

      <section aria-labelledby="registry">
        <h2 id="registry">Browser registry (getTools)</h2>
        <p>
          <code>{SPIKE_TOOL_NAME}</code> registrations:{" "}
          <strong>{spikeToolCount}</strong>
          {spikeToolCount > 1 ? " — DUPLICATE: this candidate fails the gate." : ""}
        </p>
        <ul>
          {registered.map((entry) => (
            <li key={`${entry.origin}:${entry.name}`}>
              {entry.name} — readOnlyHint={String(entry.annotations?.readOnlyHint ?? false)}
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="invocations">
        <h2 id="invocations">Invocations</h2>
        <p>
          Invoke <code>{SPIKE_TOOL_NAME}</code> from the DevTools WebMCP panel, then
          check whether the execution signal arrived.
        </p>
        <ul>
          {invocations.map((report, index) => (
            <li key={index}>
              signal forwarded: <strong>{String(report.signalPresent)}</strong>, aborted:{" "}
              {String(report.aborted)}
            </li>
          ))}
        </ul>
        {invocations.length > 0 && !invocations.every((report) => report.signalPresent) ? (
          <p role="alert">
            This path did not forward the execution signal. It cannot carry
            cancellation-sensitive tools; use direct native registration for those.
          </p>
        ) : null}
      </section>

      <section aria-labelledby="log">
        <h2 id="log">Log</h2>
        <ol>
          {lines.map((line) => (
            <li key={line.seq}>{line.text}</li>
          ))}
        </ol>
      </section>
    </main>
  );
}
