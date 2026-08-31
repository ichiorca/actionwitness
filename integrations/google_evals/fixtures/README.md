# Checked-in evaluator fixtures (Tier 2 — AC-16, FR-101)

This directory holds the redacted, version-pinned `webmcp-evals` JSON report
fixture(s) used by CI and by the offline benchmark fallback. Requirements:

- generated from the frozen benchmark manifest against the pinned evaluator version;
- at least three scenarios × three completed trials, including one call-level
  pass whose deterministic outcome fails (the `silent_outcome_defect` trial);
- redacted before commit; always labeled `recorded_fixture`, never `live`.
