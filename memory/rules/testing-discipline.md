---
title: Testing discipline
scope: project
---

- Every behavior change ships with tests — add or update them in the same change.
- Structure tests Arrange–Act–Assert; name them after the behavior under test.
- Test behavior through real entry points, not private implementation details.
- NEVER weaken or delete an assertion just to make a test pass — fix the code or the test's premise.
- Run the relevant tests before declaring done; report failures verbatim, don't paper over them.
- Cover the edge cases that matter (empty, boundary, error paths), not just the happy path.
- Keep tests fast and deterministic; no reliance on wall-clock, network, or order between tests.
