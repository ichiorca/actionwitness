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
