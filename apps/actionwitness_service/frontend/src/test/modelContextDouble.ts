/**
 * Deterministic `document.modelContext` test double (spec §26.3).
 *
 * jsdom does not supply WebMCP, so the frontend suite has to bring its own. This
 * implements the surface `webmcp-types` declares — `registerTool` with
 * AbortSignal-based unregistration, `getTools()`, and the `toolchange` event —
 * with no timers and no randomness, so a test asserts on state rather than
 * waiting for one.
 *
 * It records `registerCalls` separately from the live tool set. That distinction
 * is the point: React StrictMode intentionally mounts, unmounts and remounts an
 * effect, so a correct adapter *does* call `registerTool` twice while leaving
 * exactly one tool registered. Counting only the survivors would hide a leak;
 * counting only the calls would report a false one.
 */

type Tool = WebMCP.ModelContextTool;
type Registered = WebMCP.RegisteredTool;

export interface RegistrationRecord {
  readonly name: string;
  readonly aborted: boolean;
}

export class ModelContextDouble extends EventTarget implements WebMCP.ModelContext {
  ontoolchange: ((this: WebMCP.ModelContext, ev: Event) => unknown) | null = null;

  /** Every registerTool call, in order, including ones later unregistered. */
  readonly registerCalls: RegistrationRecord[] = [];

  /** Every toolchange dispatched, so a test can assert churn is bounded. */
  toolChangeCount = 0;

  readonly #tools = new Map<string, { registered: Registered; tool: Tool }>();

  async registerTool(
    tool: Tool,
    options?: WebMCP.ModelContextRegisterToolOptions,
  ): Promise<void> {
    const record: { name: string; aborted: boolean } = { name: tool.name, aborted: false };
    this.registerCalls.push(record);

    if (options?.signal?.aborted === true) {
      // Already-aborted signal: the browser must not leave a tool behind.
      record.aborted = true;
      return;
    }

    this.#tools.set(tool.name, {
      tool,
      registered: {
        name: tool.name,
        title: tool.title ?? tool.name,
        description: tool.description,
        ...(tool.inputSchema === undefined ? {} : { inputSchema: tool.inputSchema }),
        window: globalThis.window,
        origin: globalThis.location?.origin ?? "null",
        ...(tool.annotations === undefined ? {} : { annotations: tool.annotations }),
      },
    });
    this.#emitToolChange();

    options?.signal?.addEventListener(
      "abort",
      () => {
        record.aborted = true;
        this.#tools.delete(tool.name);
        this.#emitToolChange();
      },
      { once: true },
    );
  }

  async getTools(): Promise<Registered[]> {
    return [...this.#tools.values()].map((entry) => entry.registered);
  }

  /** Invoke a registered tool the way an agent would, including its abort signal. */
  async invoke(
    name: string,
    input: Record<string, unknown> = {},
    signal: AbortSignal = new AbortController().signal,
  ): Promise<unknown> {
    const entry = this.#tools.get(name);
    if (entry === undefined) {
      throw new Error(`no tool registered as ${name}`);
    }
    return entry.tool.execute(input, { signal });
  }

  /**
   * Invoke the way the pinned Chrome build actually does: NO context argument
   * at all. ADR-0002 recorded that `executeTool` forwards no per-invocation
   * signal, and the Tier 1 gate run (2026-09-01) proved an adapter that
   * assumes a context crashes on every native invocation there. Tests that
   * only ever used `invoke()` reproduced the double's optimism, not the
   * browser.
   */
  async invokeAsPinnedBuild(name: string, input: Record<string, unknown> = {}): Promise<unknown> {
    const entry = this.#tools.get(name);
    if (entry === undefined) {
      throw new Error(`no tool registered as ${name}`);
    }
    return (
      entry.tool.execute as unknown as (args: Record<string, unknown>) => Promise<unknown>
    )(input);
  }

  get toolNames(): string[] {
    return [...this.#tools.keys()];
  }

  #emitToolChange(): void {
    this.toolChangeCount += 1;
    const event = new Event("toolchange");
    this.ontoolchange?.call(this, event);
    this.dispatchEvent(event);
  }
}

export interface InstalledDouble {
  readonly modelContext: ModelContextDouble;
  /** Restore the previous (usually absent) `document.modelContext`. */
  readonly uninstall: () => void;
}

/**
 * Install the double on `document`. Always pair with `uninstall()` in a teardown:
 * a leaked double makes the unsupported-browser tests silently meaningless.
 */
export function installModelContextDouble(): InstalledDouble {
  const modelContext = new ModelContextDouble();
  const had = "modelContext" in document;
  const previous = (document as Document & { modelContext?: WebMCP.ModelContext })
    .modelContext;

  Object.defineProperty(document, "modelContext", {
    value: modelContext,
    configurable: true,
    writable: true,
  });

  return {
    modelContext,
    uninstall: () => {
      if (had) {
        Object.defineProperty(document, "modelContext", {
          value: previous,
          configurable: true,
          writable: true,
        });
      } else {
        delete (document as Document & { modelContext?: WebMCP.ModelContext }).modelContext;
      }
    },
  };
}
