/**
 * A conformant `document.modelContext`, installed before the app boots.
 *
 * Chromium ships no WebMCP without the flag ADR-0002 pins, and §26.4 keeps the
 * flagged build a manual checklist. That leaves a gap this lane can close
 * honestly: **register through the browser's real registry, invoke through the
 * page's real handlers, and let every request, response, cookie and database
 * write be the production ones.** The only substitute is the registry itself —
 * the same substitution `src/test/modelContextDouble.ts` makes for jsdom.
 *
 * What that buys, and what it does not:
 *
 * - It **does** exercise `useWebMCP`'s registration and unregistration, the
 *   native path's `AbortSignal` forwarding, `getTools()` reconciliation, the
 *   `toolchange` witness, result and error normalization, the confirmation
 *   promise that stays pending across a human decision, and every server
 *   route those reach.
 * - It **does not** prove the pinned Chrome build behaves this way. That is
 *   what `tests/browser/webmcp-spike-checklist.md` is for, and this lane does
 *   not claim to replace it.
 *
 * Two fidelity decisions are load-bearing:
 *
 * **Hints live under `annotations`.** `webmcp-types` puts `readOnlyHint` and
 * `untrustedContentHint` inside `RegisteredTool.annotations`, so this does too —
 * even though the product's `describeTool` reads them from the top level and
 * therefore always captures `null`. Mirroring the product's expectation instead
 * would have hidden that disagreement rather than exposed it.
 *
 * **`invokePinned` passes no second argument.** ADR-0002 recorded that the
 * pinned build's `executeTool` forwards no per-invocation context, and the Tier
 * 1 gate run proved an adapter that assumes one crashes there. A lane that only
 * ever invoked with a context would reproduce this file's optimism rather than
 * the browser's behaviour.
 */

/** How one in-flight invocation looks to the test that started it. */
export interface AgentInvocation {
  readonly state: "pending" | "fulfilled" | "rejected";
  /** The normalized tool result, or `null` while pending or rejected. */
  readonly value: unknown;
  /** The rejection message, or `null` otherwise. */
  readonly error: string | null;
}

/** The page-side surface the specs drive. Mirrors an agent, not a user. */
export interface WebMcpAgent {
  toolNames(): string[];
  describe(): { name: string; description: string; inputSchema: unknown }[];
  toolChangeCount(): number;
  registerCallCount(): number;
  /** Invoke the way the pinned build does: arguments only, no context. */
  invokePinned(name: string, args?: Record<string, unknown>): Promise<unknown>;
  /** Invoke with a context, forwarding a signal the test can abort. */
  start(handle: string, name: string, args?: Record<string, unknown>): void;
  abort(handle: string): void;
  poll(handle: string): AgentInvocation;
  /** Register a look-alike tool the way a third-party script would. */
  injectTool(name: string, description: string, inputSchema: Record<string, unknown>): Promise<void>;
}

declare global {
  interface Window {
    __awAgent?: WebMcpAgent;
  }
}

/**
 * The init script. Serialized by Playwright, so it must reference nothing
 * outside its own body.
 */
export function installWebMcpAgent(): void {
  interface Entry {
    tool: {
      name: string;
      title?: string;
      description: string;
      inputSchema?: object;
      annotations?: { readOnlyHint?: boolean; untrustedContentHint?: boolean };
      execute: (args: Record<string, unknown>, options?: { signal: AbortSignal }) => unknown;
    };
    registered: Record<string, unknown>;
  }

  const tools = new Map<string, Entry>();
  const pending = new Map<
    string,
    { controller: AbortController; state: string; value: unknown; error: string | null }
  >();
  const target = new EventTarget();
  let toolChangeCount = 0;
  let registerCallCount = 0;

  const emitToolChange = (): void => {
    toolChangeCount += 1;
    const event = new Event("toolchange");
    const handler = modelContext.ontoolchange;
    if (typeof handler === "function") {
      handler.call(modelContext, event);
    }
    target.dispatchEvent(event);
  };

  const modelContext = {
    ontoolchange: null as ((ev: Event) => unknown) | null,

    addEventListener: (
      type: string,
      listener: EventListenerOrEventListenerObject,
      options?: boolean | AddEventListenerOptions,
    ): void => {
      target.addEventListener(type, listener, options);
    },
    removeEventListener: (
      type: string,
      listener: EventListenerOrEventListenerObject,
      options?: boolean | EventListenerOptions,
    ): void => {
      target.removeEventListener(type, listener, options);
    },
    dispatchEvent: (event: Event): boolean => target.dispatchEvent(event),

    registerTool: (tool: Entry["tool"], options?: { signal?: AbortSignal }): Promise<void> => {
      registerCallCount += 1;

      // An already-aborted signal must leave nothing behind, or a StrictMode
      // remount would register a tool nobody can unregister.
      if (options?.signal?.aborted === true) {
        return Promise.resolve();
      }

      tools.set(tool.name, {
        tool,
        registered: {
          name: tool.name,
          title: tool.title ?? tool.name,
          description: tool.description,
          ...(tool.inputSchema === undefined ? {} : { inputSchema: tool.inputSchema }),
          window,
          origin: location.origin,
          // Nested, per `webmcp-types`. See the module docstring.
          ...(tool.annotations === undefined ? {} : { annotations: tool.annotations }),
        },
      });
      emitToolChange();

      options?.signal?.addEventListener(
        "abort",
        () => {
          // Only if this registration is still the live one: a re-registration
          // under the same name replaced it, and the old signal aborting
          // afterwards must not delete the replacement.
          if (tools.get(tool.name)?.tool === tool) {
            tools.delete(tool.name);
            emitToolChange();
          }
        },
        { once: true },
      );

      return Promise.resolve();
    },

    getTools: (): Promise<Record<string, unknown>[]> =>
      Promise.resolve([...tools.values()].map((entry) => entry.registered)),
  };

  Object.defineProperty(document, "modelContext", {
    value: modelContext,
    configurable: true,
    writable: true,
  });

  const entryOf = (name: string): Entry => {
    const entry = tools.get(name);
    if (entry === undefined) {
      throw new Error(`no tool registered as ${name}; registered: ${[...tools.keys()].join(", ")}`);
    }
    return entry;
  };

  window.__awAgent = {
    toolNames: () => [...tools.keys()],
    describe: () =>
      [...tools.values()].map((entry) => ({
        name: entry.tool.name,
        description: entry.tool.description,
        inputSchema: entry.tool.inputSchema ?? {},
      })),
    toolChangeCount: () => toolChangeCount,
    registerCallCount: () => registerCallCount,

    invokePinned: async (name: string, args: Record<string, unknown> = {}) => {
      // No second argument, deliberately. ADR-0002: the pinned build forwards
      // no per-invocation context.
      const execute = entryOf(name).tool.execute as (a: Record<string, unknown>) => unknown;
      return await execute(args);
    },

    start: (handle: string, name: string, args: Record<string, unknown> = {}) => {
      const controller = new AbortController();
      const record = {
        controller,
        state: "pending",
        value: null as unknown,
        error: null as string | null,
      };
      pending.set(handle, record);
      void Promise.resolve(entryOf(name).tool.execute(args, { signal: controller.signal })).then(
        (value) => {
          record.state = "fulfilled";
          record.value = value;
        },
        (error: unknown) => {
          record.state = "rejected";
          record.error = error instanceof Error ? error.message : String(error);
        },
      );
    },

    abort: (handle: string) => {
      const record = pending.get(handle);
      if (record === undefined) {
        throw new Error(`no invocation started as ${handle}`);
      }
      record.controller.abort();
    },

    poll: (handle: string) => {
      const record = pending.get(handle);
      if (record === undefined) {
        throw new Error(`no invocation started as ${handle}`);
      }
      return {
        state: record.state as AgentInvocation["state"],
        value: record.value,
        error: record.error,
      };
    },

    injectTool: async (
      name: string,
      description: string,
      inputSchema: Record<string, unknown>,
    ) => {
      // No cleanup signal: a third-party injection does not offer one, and the
      // point of the surface witness is that it is seen regardless.
      await modelContext.registerTool({
        name,
        description,
        inputSchema,
        execute: () =>
          Promise.resolve({
            content: [{ type: "text", text: "injected by the e2e lane; nothing was performed" }],
          }),
      });
    },
  };
}

export {};
