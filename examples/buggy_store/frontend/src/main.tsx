/**
 * Browser entry point for the standalone storefront.
 *
 * `StrictMode` is deliberate: it double-invokes effects, which is how the
 * cancellation guard in `App`'s loader gets exercised in development rather
 * than discovered in production.
 *
 * The shopper identity lives in `localStorage`. It scopes a cart to a browser
 * and nothing more — it is not authorization, and this demo store makes no
 * security claim about it (§20.1 reserves that role for the harness's cookie).
 * Losing it loses a cart, which is the worst it can do.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { StoreClient } from "./api";

const WORKSPACE_KEY = "buggy-store.workspace-id";

function shopperWorkspace(): string {
  try {
    const existing = window.localStorage.getItem(WORKSPACE_KEY);
    if (existing !== null && existing.length > 0) return existing;
    const created = `shopper-${Math.random().toString(36).slice(2, 12)}`;
    window.localStorage.setItem(WORKSPACE_KEY, created);
    return created;
  } catch {
    // Private windows and blocked site data throw on access. A per-load
    // identity still works; the shopper just gets a fresh cart.
    return `shopper-${Math.random().toString(36).slice(2, 12)}`;
  }
}

const root = document.getElementById("root");
if (root === null) throw new Error("missing #root");

createRoot(root).render(
  <StrictMode>
    <App client={new StoreClient(shopperWorkspace())} />
  </StrictMode>,
);
