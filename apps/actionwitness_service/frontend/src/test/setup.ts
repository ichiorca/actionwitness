import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// Unmount between tests so a leaked React tree cannot register WebMCP tools that
// outlive their test. Registration leaks are the exact failure the lifecycle
// adapter exists to prevent, so the suite must not create them itself.
afterEach(() => {
  cleanup();
});