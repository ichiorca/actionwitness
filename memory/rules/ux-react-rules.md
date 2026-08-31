---
title: React UI rules
scope: project
---

**IRON LAW: NEVER LET POLLING OR AGENT ACTIONS ERASE HUMAN INPUT, FOCUS, OR CONSENT.**

Violating the letter of these rules is violating the spirit of these rules.

- MUST keep one authoritative workspace/run snapshot owner above the declared panels; MUST pass typed panel data and intent callbacks downward; NEVER mirror server contract, phase, guidance, confirmation, or verdict into competing component state.
- MUST keep local state to unsaved drafts, disclosure, focus restoration, filters, and transient request status; MUST derive labels, enabled controls, counts, and presentation during render; NEVER use an Effect to synchronize derived state.
- MUST keep dirty form drafts separate from persisted snapshots; MUST preserve and disclose conflicts when polling returns newer data; NEVER key forms, controls, findings, or timeline rows by polling revision, array index, or any value other than semantic identity or immutable server ID.
- MUST scope polling responses to the active workspace/run, ignore or abort obsolete responses, deduplicate timeline events by immutable ID, preserve the last confirmed snapshot during refresh, and stop terminal-state polling; NEVER replace useful content with a blank loading state.
- MUST keep each declared panel a focused view component and keep fetch/poll/mutation orchestration above sibling panels; NEVER add shared client state or a global store merely to distribute one server-owned workspace response.
- MUST preserve a complete human path when WebMCP is absent or registration fails; MUST expose agent-caused state, confirmations, errors, and recovery actions in the ordinary DOM and accessibility tree; NEVER treat an agent tool result as the displayed business verdict.
- MUST use native landmarks, headings, forms, labels, fieldsets, buttons, lists, and tables before ARIA; MUST render journey events as an ordered list and expected/actual evidence with explicit structure; NEVER use clickable `div` elements or positive `tabIndex`.
- MUST distinguish acknowledged, committed, independently verified, cancelled, waiting, and failed states with text, not color alone; MUST announce concise actor, phase, waiting, verification, and error changes from a persistent polite status region; NEVER announce every polled timeline event or use assertive alerts for routine updates.
- MUST render protected confirmations from the server confirmation object as genuinely modal workflows: inert background, visible title and cancel control, contained focus, Escape when cancellation is allowed, least-destructive initial focus for irreversible actions, and focus restoration to the invoker or logical successor; NEVER bind ambiguous Enter handling to approval.
- MUST test roles and visible names plus real keyboard/focus sequences, polling updates, dirty-draft preservation, agent-originated confirmations, and dialog restoration; MUST run `npm test` and `npm run build` from `apps/actionwitness_service/frontend`; NEVER claim axe, Playwright, screen-reader, lint, or standalone type-check coverage unless the repo adds and runs those gates.

| Excuse | Reality |
|---|---|
| "Polling just refreshes the form." | It can erase the operator's intent, focus, and evidence context. |
| "The tool returned success." | ActionWitness requires the server-owned observation and verdict. |
| "`aria-modal` makes it modal." | Modality requires inertness, focus containment, cancellation behavior, and restoration. |
| "The icon and color are obvious." | Status must remain named, perceivable, and operable without vision or color. |
| "Keyboard testing can wait." | Focus behavior is part of the component contract, not a later polish pass. |

Red flags — STOP:

- "This key forces a clean refresh."
- "The draft probably is not dirty."
- "The tool already confirmed success."
- "ARIA is enough for now."
- "I will test focus after the UI is finished."

<!-- sources fetched at generation: https://react.dev, https://react.dev/learn/choosing-the-state-structure, https://react.dev/learn/you-might-not-need-an-effect, https://react.dev/learn/synchronizing-with-effects, https://react.dev/learn/preserving-and-resetting-state, https://react.dev/reference/react/StrictMode, https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/, https://www.w3.org/WAI/WCAG22/Understanding/status-messages.html, https://www.w3.org/WAI/WCAG22/Understanding/keyboard.html -->
