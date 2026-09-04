# Cavesson drift gardener

Run `cavesson drift snapshot --as=baseline --adapter=claude-code` once (early) to capture a baseline. Later, run `cavesson drift snapshot --as=window --adapter=claude-code` then `cavesson drift report --baseline=baseline.json --window=window.json` to detect drift. Report any `cavesson.drift.detected` signals (L0 decisions, cost-per-call, eval pass-rate) for operator review.
