# Inspect the cavesson L1 event log

Run `cavesson events query --since=<ISO8601 a few minutes ago>` with your shell tool for a bounded snapshot of recent activity (add `--kind`/`--session` to filter). Do NOT use `cavesson events tail` here — it blocks forever; only use tail for a deliberate live follow.

Summarize the recent activity for the operator: notable tool calls, authorization decisions, hook fires, cache/idempotency/backpressure events, and cost ticks. Flag anything denied or short-circuited.
