# 013 — plan

Round-2's own costing: ~0.5 day, "do this one regardless of everything else."
The scaffold agrees — snapshots, policy, classification, report block,
minimizer special-case, and replay preservation all exist; only the diff and
partition are missing.

1. **Core diff** (`actionwitness_core.engine`): recursive walk over the two
   canonical documents producing `(path, kind, before, after)` tuples; kinds
   added/removed/changed; arrays compared positionally (canonical form is
   already deterministic); bounded value excerpts via the existing redaction
   summaries so the report never carries unbounded payloads.
2. **Partition**: inputs are the contract's assertion paths (already parsed
   dotted paths), each *executed* tool's declared effect prefixes (the 003
   effect map already publishes them), and `allow_paths`. Prefix matching
   reuses the restricted-path resolver, not string startswith.
3. **Policy + report**: replace the `not_evaluated` stub's early return;
   populate `undeclared_changes`; classification joins the critical set so
   §24's set-equality expectations see it.
4. **Injector**: store-side `undeclared_side_effect` (e.g. the fault mutates
   `preferences` alongside a correct cart mutation — §13.2's document already
   carries `preferences` precisely so observation can see it); template
   asserts the cart outcome and carries `no_undeclared_changes`.
5. **UI**: one panel section listing undeclared paths, server-driven like
   every 006 panel.

Risks: array-diff semantics (keep positional; document); classification-set
growth breaking existing eval expectations (only cases GENERATED after this
change carry it — regenerate none); performance is a non-issue at §13.2
document sizes.

**Timing**: the one round-2 item plausibly landable pre-submission — enter
only after 009-T9/T11 and the demo are done. Otherwise first post-submission.

## Deviations and decisions worth an operator's eye

_Per-task, anchored to spec sections — the standing convention._

### Raised by this milestone

- **T1 — §17.2's decimal rule is applied *within* a JSON type, never across
  one.** The rule's stated purpose is that "a target that reformats a decimal"
  must not produce a spurious failure, so `"20.00"` and `"20.0"` compare equal,
  as do `20` and `20.0`. A path that moved from the number `20` to the string
  `"20"` is reported as changed: that is a shape change, not a formatting one,
  and suppressing it would hide a target quietly altering its output contract.
  Over-reporting is visible and waivable through `allow_paths`; under-reporting
  is neither. **Worth an operator's eye** — the spec does not settle the
  cross-type case explicitly.

- **T1 — a document key that is not a legal path segment is reported at its
  parent.** `_SEGMENT` admits identifier-shaped keys and integers; a key with a
  space or a leading digit cannot be named in a dotted path, because the path
  would not parse back to itself. Skipping it would let a change vanish, and
  raising would fail verification on a document a target is entitled to return.
  At the top level there is no parent, so an invalid *namespace* raises —
  `Observation` validates those as `Token`, so reaching it means something
  bypassed that validation.

- **T1 — `ChangeKind` is deliberately not in the exported registry.** The
  registry exists for vocabularies that cross the API boundary; §23.1's block
  carries paths and counts, and no payload names a change kind. `registry.json`
  is a committed artifact and should gain rows only when something reads them.

- **T2 — no production change was needed.** The partition, its `allow_paths`
  handling, and its use of the restricted-path resolver already existed and were
  correct; they were untested at the boundary only because `changed_paths` was
  always `None`. The gap was tests that distinguish the implementation from the
  wrong one — a `str.startswith` partition passed every pre-existing test in the
  file while declaring `target.cartridge` covered by `target.cart`.

- **T3 — `undeclared_changes` is absent, not zeroed, when the policy did not
  evaluate.** A block reading "0 changed, 0 undeclared" says *nothing changed*
  where the truth is *nothing was compared*. Absent too when the contract
  carries no such policy, so a run that never asked the question does not appear
  to have answered it.

- **T4 — `declared_contract_paths` moved into the core.** Verification and
  replay both need §9.10(a)'s rule, and two implementations of "what did the
  contract declare" is exactly how a replay repartitions identical snapshots and
  reports a classification the source run never produced — surfacing under
  §24.1's set equality as a false regression.

- **T4 — a latent seeding trap, found and gated.** Template seeding hashes the
  document *as written*; §24.2 step 6 re-verifies a source contract by hashing
  the parsed contract's canonical form. A field left to its default —
  `allow_paths` on `no_undeclared_changes` — is absent from the first and present
  in the second, so eval-case generation refuses the run with "the source
  contract does not match its stored hash". The failure surfaces far from its
  cause: the contract validates, the run arms, the journey succeeds,
  verification is correct, and only case generation breaks. Every template is
  now asserted to be written in canonical form. **Worth an operator's eye** —
  the durable fix is for seeding to hash the parsed form, which is a change to
  004's contract-seeding path and out of 013's scope.

- **T5 — the scenario is read inside `_apply_quantity`.** One tool call must
  produce one state version; injecting the side effect as a second
  `with_target_state` would move §13.2's monotonic counter by two, and FR-032
  treats a version change as evidence that state moved.

- **T5 — the injected note is a fixed string.** A generated value would differ
  between two otherwise identical runs and break §24's canonical-document
  comparison for a reason unrelated to the defect.

- **T6 — the findings payload now carries `paths` and `applied_exemptions`.** It
  previously exposed only the first path of a multi-path finding, which is the
  wrong shape for the one classification §17.1 defines as one-finding-many-paths.
  Additive; `path` is unchanged.

- **T7 — criterion 6 is discharged by running the gates, not by a test.** A
  suite cannot assert that it passed. The traceability map names the lane gate
  that fails if a lane stops selecting tests, since a marker typo silently
  deselecting a lane is how "all green" becomes true by running nothing.

### Not done in this milestone

- **FR-159's causal attribution is not implemented.** The finding names the
  paths and records applied exemptions, but does not yet attribute a likely
  cause by adjacency — "the last terminal action completing immediately before
  the change first appears in a recorded state version, a recorded human
  confirmation decision, or `none`". §22's `attributed_cause_json` column exists
  and stays null. None of 013's six exit criteria names attribution, and the
  demonstration does not need it; it is the obvious next increment. **Operator
  decision: whether this blocks calling FR-159 complete.**
