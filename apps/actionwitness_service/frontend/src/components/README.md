# Workspace components (spec v1.9 §18, §8.4)

All Tier 1–2 components are implemented. Four carry enough behaviour to own a
file; the purely presentational panels share `panels.tsx`:

- `GuidanceBanner.tsx` — active actor + next action from the server's
  `GuidanceState` (LD-30, §12.13), with the "Go to this step" walk to the
  control that performs the named `action_code` `[T1]`
- `ConfirmationDialog.tsx` — focus-trapped approval for protected actions
  (§14): no preselected choice, expiry countdown beside the absolute time,
  the consequence as labelled rows with the raw JSON behind a disclosure,
  read-only in a tab that does not own the waiting call `[T1]`
- `ContractForm.tsx` — the flat declarative contract tool (§25.2, FR-021):
  the browser reads `create_outcome_contract` off the form's own markup `[T1]`
- `WorkspaceErrorBoundary.tsx` — the failure wall around the workspace `[T1]`
- `panels.tsx` — the presentational panels, all server-state driven:
  `CapabilityBar`, `ConfigPanel` (scenario mode + adapter-advertised fault
  profiles), `ToolRegistrationPanel` (FR-003), `ContractPanel`, `TargetPanel`,
  `RunTimeline` (LD-19),
  `FindingsPanel`, `UndeclaredChangesPanel` (§9.10), `ToolSurfacePanel`
  (FR-169), `ComparisonPanel` (§23.7), `EvalPanel` `[T1–T2]`, and
  `BenchmarkPanel` (§23.5) `[T2]` — exported and tested, **not yet rendered
  by `App.tsx`** (spec 008's import UI is still a scope decision; see
  `e2e/README.md`)

Layout note: `App.tsx` arranges these behind a left rail — a Workflow view
(Contract → Run → Verdict → Regression stages) and an Administration view
(`ConfigPanel` + `ToolRegistrationPanel`). Both views stay mounted so the
WebMCP surface never changes shape with navigation.
