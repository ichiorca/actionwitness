import React from "react";
import { createRoot } from "react-dom/client";

import SpikeHarness from "./SpikeHarness";

// StrictMode is mandatory here, not incidental: its deliberate double-mount is
// the condition the M0 exit gate names ("registers and cleans up one read-only
// test tool without StrictMode duplication"). Running the spike without it would
// measure the wrong thing.
const container = document.getElementById("spike-root");
if (container === null) {
  throw new Error("spike root element is missing");
}

createRoot(container).render(
  <React.StrictMode>
    <SpikeHarness />
  </React.StrictMode>,
);