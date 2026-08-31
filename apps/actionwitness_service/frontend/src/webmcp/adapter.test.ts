import { afterEach, describe, expect, it } from "vitest";

import { isWebMcpSupported, registerHarnessTool } from "./adapter";

/**
 * The first §25.1 lifecycle-adapter compatibility case: the unsupported
 * environment. jsdom supplies no `document.modelContext`, which makes it an
 * accurate stand-in for a browser without WebMCP — the configuration the whole
 * human UI must keep working in (spec §26.3, AC-09).
 *
 * The remaining §25.1 cases — mount/unmount cleanup, StrictMode double-mount,
 * `enabled` transitions, getTools()/toolchange reconciliation, normalized
 * success and thrown-error results, registration-failure display — arrive with
 * the ADR-0002 pin, because their assertions depend on which hook is selected.
 */
describe("webmcp adapter — unsupported environment", () => {
  afterEach(() => {
    delete (document as Partial<Document> & { modelContext?: unknown }).modelContext;
  });

  it("reports WebMCP as unsupported when document.modelContext is absent", () => {
    expect("modelContext" in document).toBe(false);
    expect(isWebMcpSupported()).toBe(false);
  });

  it("detects support from the presence of document.modelContext, not a user agent string", () => {
    Object.defineProperty(document, "modelContext", {
      value: { registerTool: () => undefined },
      configurable: true,
    });

    expect(isWebMcpSupported()).toBe(true);
  });

  it("fails loudly rather than silently no-opping until the ADR-0002 pin lands", () => {
    // A scaffold that quietly returns would let application code register tools
    // that never exist. Registration must fail visibly (spec §25.1).
    expect(() => registerHarnessTool()).toThrowError(/spike/i);
  });
});