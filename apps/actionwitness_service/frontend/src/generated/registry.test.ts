import { describe, expect, it } from "vitest";

import registry from "./registry.json";

/**
 * The registry exists so handler, UI, and test names cannot fork (AC-6). That
 * only holds if the frontend really imports it rather than re-typing the strings,
 * so this test asserts the import path works and the artifact has the shape the
 * workspace UI will read.
 *
 * The Python side owns correctness — `tests/unit/test_registry.py` fails if this
 * file drifts from its source. Here we only prove it is reachable and usable.
 */
describe("generated name registry", () => {
  it("is importable and versioned", () => {
    expect(registry.schema_version).toBe(1);
  });

  it("carries the closed run-state vocabulary the workspace UI renders", () => {
    const runStates = Object.keys(registry.enums.run_state.members);
    expect(runStates).toContain("armed");
    expect(runStates).toContain("awaiting_confirmation");
    expect(runStates).toContain("passed_with_warnings");
  });

  it("carries error codes with the retryability the client must branch on", () => {
    const lockTimeout = registry.error_codes.WORKSPACE_LOCK_TIMEOUT;
    expect(lockTimeout.retryable).toBe(true);

    // A refused intent must never invite a re-send that could duplicate a mutation.
    const alreadyVerifying = registry.error_codes.RUN_ALREADY_VERIFYING;
    expect(alreadyVerifying.http_status).toBe(409);
    expect(alreadyVerifying.retryable).toBe(false);
  });

  it("says it is generated, so a hand-edit is visibly wrong", () => {
    expect(registry["//"]).toMatch(/do not edit/i);
  });
});