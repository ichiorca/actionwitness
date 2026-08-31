# Required Tier 1+ components (spec v1.9 §18, §8.4)

To be created as their tier begins — no placeholders committed for UI:

- `GuidanceBanner.tsx` — active actor + next action (LD-30, §12.13) `[T1]`
- `ConfigPanel.tsx` — target, scenario mode (`pre_fix`/`post_fix`), failure profile `[T1]`
- `ContractPanel.tsx` — template selector, validation errors, arm action `[T1]`
- `TargetPanel.tsx` — Buggy Store human view (read-only during armed runs, LD-20) `[T1]`
- `RunTimeline.tsx` — ordered events via paged polling (LD-19) `[T1]`
- `ConfirmationDialog.tsx` — focus-trapped approval for protected actions (§14) `[T1]`
- `FindingsPanel.tsx` — layered verdict, expected/actual, eval actions `[T1]`
- `ComparisonPanel.tsx` — matched pre/post-fix comparison (§23.7) `[T1]`
- `EvalPanel.tsx` — case hash, environment, expectation vs outcome `[T2]`
- `BenchmarkPanel.tsx` — 2×2 matrix, metrics, coverage, source labels (§23.5) `[T2]`
