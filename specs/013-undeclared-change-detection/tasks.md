# 013 — tasks

Cite the T-ID in every commit that advances it.

- [ ] T1 — Core canonical diff: deterministic changed-path tuples over two
      snapshots, bounded excerpts, byte-identical output for identical input.
- [ ] T2 — Declared/undeclared partition: assertion paths + executed tools'
      effect prefixes + `allow_paths`, via the restricted-path resolver.
- [ ] T3 — `no_undeclared_changes` evaluates: fail on undeclared paths,
      `not_evaluated` only without snapshots; populate `undeclared_changes`
      and the `undeclared_state_change` classification.
- [ ] T4 — Replay parity: a generated case carrying the policy reproduces
      the identical partition and classification set.
- [ ] T5 — Store `undeclared_side_effect` injector + third contract
      template; template-honesty test extended to three.
- [ ] T6 — "Changed outside contract" panel, server-driven.
- [ ] T7 — Exit gate: acceptance test where every named assertion passes and
      the run fails on the side effect alone; extend the traceability map.
