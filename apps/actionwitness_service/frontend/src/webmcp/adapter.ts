/**
 * Local WebMCP lifecycle adapter — the ONLY module application code may import
 * for tool registration (spec v1.8 §25.1, §19.1).
 *
 * Contract to implement after the §25.1 hook spike selects exactly one package:
 *  - unsupported environment: every call is a safe no-op (`document.modelContext` absent);
 *  - mount/unmount and React StrictMode double-mount cleanup without duplicate registration;
 *  - registration status reconciled via `document.modelContext.getTools()` + `toolchange` (FR-003);
 *  - normalized success/error result shapes (`isError: true` envelope — §11.4);
 *  - per-invocation execution `signal` forwarded to handlers; cancellation-sensitive
 *    tools (`proceed_to_checkout`) use direct native registration if the hook
 *    does not expose it (FR-037, LD-4);
 *  - direct `document.modelContext.registerTool` exposed as a supported fallback.
 *
 * Scaffolding only — intentionally throws until the spike decision is recorded.
 */

export interface NormalizedToolResult {
  content: Array<{ type: "text"; text: string }>;
  isError?: boolean;
}

export function isWebMcpSupported(): boolean {
  return typeof document !== "undefined" && "modelContext" in document;
}

export function registerHarnessTool(): never {
  throw new Error(
    "webmcp adapter not implemented: complete the spec §25.1 hook spike first and pin one package",
  );
}
