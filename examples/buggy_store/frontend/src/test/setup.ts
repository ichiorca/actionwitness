/**
 * Vitest setup for the storefront suite.
 *
 * Note what is *not* here: no `document.modelContext`, and no WebMCP double.
 * The harness suite installs one because its adapter is under test; this
 * storefront must work in a browser that has never heard of WebMCP, so the
 * absence is the condition under test rather than a gap to fill.
 */

import "@testing-library/react";
