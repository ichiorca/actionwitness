/**
 * Types for `actionwitness-bridge.js`, so the strict harness suite can exercise
 * it.
 *
 * The bridge itself is plain, unbuilt JavaScript on purpose (see its header): it
 * runs in somebody else's theme, and a merchant has to be able to read the file
 * they are pasting in. That leaves the repository's only JavaScript test runner
 * — the harness frontend's vitest lane — as the place its logic can be
 * exercised, and this declaration is what lets a `strict` TypeScript test import
 * a `.js` file without an `any` or a cast anywhere.
 *
 * **The interfaces below are structural minimums, not the DOM's own types.** The
 * bridge asks for a window with five members and a response with six, so that is
 * what it is typed against: a real `Window` and a real `Response` satisfy them,
 * and so does a test double built by hand. Typing this against `Window` instead
 * would leave the test with a cast as its only way in — which the constitution
 * forbids at exactly the boundary this file exists to describe.
 *
 * Hand-written rather than generated, so it is a claim that has to stay true.
 * `shopifyBridge.test.ts` calls every member below; a signature that drifted
 * from the implementation fails there rather than lying here.
 */

export {};

/** One `#`-fragment pairing, as `readAndStripPairing` recovers it. */
export interface BridgePairing {
  pairingId: string;
  credential: string;
}

/** §16.5's per-state guidance: who acts, what happens next, and the way out. */
export interface PairingGuidance {
  readonly actor: string;
  readonly next: string;
  readonly recovery: string;
}

/** What both tabs render (§14). Carries no credential, by construction. */
export interface BridgeView {
  readonly state: string;
  readonly actor: string;
  readonly next: string;
  readonly recovery: string;
  readonly pairingSuffix: string;
  readonly expiresAt: string | null;
  readonly runId: string | null;
}

/** Everything the bridge reads off its host window, and nothing more. */
export interface BridgeWindow {
  readonly document: Document;
  readonly location: {
    readonly href: string;
    readonly origin: string;
    readonly pathname: string;
    readonly search: string;
    hash: string;
  };
  readonly history: {
    readonly state: unknown;
    replaceState(state: unknown, unused: string, url: string): void;
  };
  readonly navigator: unknown;
  /** Absent on a non-Shopify page; `routes.root` is the locale prefix (FR-112). */
  readonly Shopify?: { readonly routes?: { readonly root?: string } };
  setTimeout(handler: () => void, ms: number): number;
  clearTimeout(id: number): void;
}

/** The parts of a `fetch` response the bridge inspects before trusting a body. */
export interface BridgeResponse {
  readonly ok: boolean;
  readonly status: number;
  readonly url: string;
  readonly redirected: boolean;
  readonly headers: { get(name: string): string | null };
  text(): Promise<string>;
}

export type BridgeFetch = (
  url: string,
  init: Record<string, unknown>,
) => Promise<BridgeResponse>;

/** The WebMCP surface the bridge uses: register, and abort to unregister. */
export interface BridgeModelContext {
  registerTool(tool: unknown, options: { signal: AbortSignal }): Promise<unknown>;
}

export interface BridgeEnvironment {
  readonly window: BridgeWindow;
  readonly fetch: BridgeFetch;
  readonly now: () => Date;
}

export interface BridgeOptions {
  readonly window: BridgeWindow;
  readonly fetch: BridgeFetch;
  readonly harnessOrigin: string;
  readonly storeOrigin: string;
  readonly now?: () => Date;
  readonly resolveModelContext?: () => BridgeModelContext | null;
  readonly onChange?: (view: BridgeView) => void;
}

export interface BridgeToolResult {
  readonly content: ReadonlyArray<{ readonly type: string; readonly text: string }>;
  readonly isError?: boolean;
}

export interface Bridge {
  start(pairing: BridgePairing): Promise<BridgeView>;
  verify(): Promise<BridgeToolResult>;
  attachPanel(): void;
  dispose(): void;
  view(): BridgeView;
  isToolRegistered(): boolean;
}

export interface ActionWitnessBridgeApi {
  readonly BRIDGE_VERSION: string;
  readonly MAX_CART_BYTES: number;
  readonly TOOL_NAME: string;
  readonly PAIRING_GUIDANCE: Readonly<Record<string, PairingGuidance>>;
  createBridge(options: BridgeOptions): Bridge;
  readAndStripPairing(win: BridgeWindow): BridgePairing | null;
  resolveCartUrl(win: BridgeWindow, storeOrigin: string): string;
  readCartDocument(
    env: BridgeEnvironment,
    storeOrigin: string,
  ): Promise<{ cart: Record<string, unknown>; capturePath: string }>;
  requireHttpsOrigin(value: string, what: string): string;
  pairingSuffix(pairingId: string): string;
  isTerminal(state: string): boolean;
  readonly instance: Bridge | null;
}

declare global {
  // eslint-disable-next-line no-var
  var ActionWitnessBridge: ActionWitnessBridgeApi | undefined;
}
