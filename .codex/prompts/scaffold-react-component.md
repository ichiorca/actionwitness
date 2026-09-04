<!-- Use when the ActionWitness React workspace needs a new reusable TypeScript component, especially a named panel, control, status display, form element, or interaction with explicit props and component-level behavior coverage. Not for Python-owned business logic, direct WebMCP registration, or the standalone Buggy Store frontend. -->

Treat `$ARGUMENTS` as a PascalCase component name followed by optional behavior, props, and accessibility requirements.

1. Parse `$ARGUMENTS`. Require a valid PascalCase component name such as `GuidanceBanner`. If the name, responsibility, or public behavior is ambiguous, ask one concise question before writing. Refuse paths outside `apps/actionwitness_service/frontend/src/components` and never overwrite an existing component.

2. Re-ground in the current frontend before editing. Read:
   - `apps/actionwitness_service/frontend/package.json`
   - `apps/actionwitness_service/frontend/tsconfig.json`
   - `apps/actionwitness_service/frontend/vite.config.ts`
   - `apps/actionwitness_service/frontend/src/App.tsx`
   - `apps/actionwitness_service/frontend/src/main.tsx`
   - `apps/actionwitness_service/frontend/src/components/README.md`
   - `apps/actionwitness_service/frontend/src/webmcp/adapter.ts` when WebMCP is relevant

   Search for the proposed component name, adjacent components, tests, imports, exports, and any API type named in `$ARGUMENTS`. Treat entries in `src/components/README.md` as planned seams, not implemented APIs.

3. Use `apps/actionwitness_service/frontend/src/components/<ComponentName>.tsx` and colocate its test at `apps/actionwitness_service/frontend/src/components/<ComponentName>.test.tsx`. Follow any newer convention discovered during step 2; otherwise use this fallback because the repository currently has no committed component-test precedent. Do not create a barrel file, stylesheet, story, or `App.tsx` integration unless `$ARGUMENTS` explicitly requires it.

4. Define the component boundary before implementation. Keep Python as the authority for contracts, workflow transitions, consent, verdicts, canonical state, and idempotency. Accept server-owned state through typed props and emit user intent through typed callbacks. Do not invent DTOs from route comments: only `/healthz` is currently implemented and the other FastAPI route modules are scaffolds.

5. If `$ARGUMENTS` mentions WebMCP, keep the component dependent on ordinary typed props and callbacks. Do not import an undeclared WebMCP hook or SDK, and do not access `document.modelContext` from the component. Direct experimental API access belongs only in `src/webmcp/adapter.ts`, whose registration implementation currently throws pending the compatibility decision.

6. Write the test first. Start the file with `// @vitest-environment jsdom` because `jsdom` is declared but no repository Vitest configuration selects a DOM environment. Import `describe`, `it`, `expect`, and only needed helpers such as `afterEach` or `vi` from `vitest`. Import `render`, `screen`, `cleanup`, and `fireEvent` only as needed from `@testing-library/react`; call `cleanup()` from `afterEach` when required.

7. Test observable behavior through the public component surface. Render representative typed props, query native roles and accessible names with `screen.getByRole` or labels with `screen.getByLabelText`, and assert rendered state or callback arguments. Add an interaction assertion when the component is interactive. Do not use snapshots, CSS-class assertions, private state assertions, `data-testid` when a semantic query works, `user-event`, jest-dom matchers, axe, or browser APIs that are not declared dependencies.

8. From `apps/actionwitness_service/frontend`, run the targeted test before creating the component and confirm that it fails for the expected missing behavior:

   `npm test -- src/components/<ComponentName>.test.tsx`

   If the runner cannot start because dependencies are absent, use the repository-documented setup command `npm install`; do not use `npm ci` because no lockfile is committed. Do not claim an observed red test when infrastructure, rather than the intended assertion, prevented it from running.

9. Implement `<ComponentName>.tsx` with the repository's existing style: double quotes, semicolons, two-space indentation, trailing commas where appropriate, and a plain default function declaration like `App.tsx`. Define and export a `<ComponentName>Props` object type or interface when the component accepts props. Mark input fields readonly where practical, use `import type` for type-only imports, and do not add a default React import unless a runtime React API is actually used.

10. Keep rendering pure. Derive display values during render, execute user-triggered work in event handlers, and add an Effect only to synchronize an external system. Make every Effect setup symmetric with cleanup so the component remains correct under the repository's enabled `React.StrictMode`. Do not mirror server-owned state into redundant local booleans or treat a tool or HTTP success response as an independently verified verdict.

11. Use semantic native HTML before ARIA: real buttons, links, labels, fieldsets, headings, lists, tables, and status text as appropriate. Keep visible labels in accessible names, expose state with text rather than color alone, preserve keyboard operation, and represent pending, failed, cancelled, and verified states distinctly when those states are in scope.

12. Run the targeted test again and make it pass without weakening the assertion:

   `npm test -- src/components/<ComponentName>.test.tsx`

13. Run every currently declared frontend gate from `apps/actionwitness_service/frontend`:

   `npm test`

   `npm run build`

   Do not invent or claim a lint or standalone typecheck result: `package.json` currently declares neither script, and the Vite build transpiles TypeScript without serving as a standalone type-check gate. Do not add dependencies or tooling scripts as part of this scaffold unless `$ARGUMENTS` explicitly requests that broader change.

14. Review the final diff. Ensure the component and test contain no placeholders, speculative API contracts, direct WebMCP access, duplicated Python business rules, unsafe assertions, or unrelated edits. Report the created files, the behavior covered by the test, the exact commands run, their results, and the absence of a declared lint/typecheck gate.
