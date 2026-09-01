# 004 — tasks

Cite the T-ID in every commit that advances it.

- [x] T1 — Migrations and schema bootstrap for the nine Tier 1 tables of §17.1
      (`workspaces`, `contracts`, `runs`, `events`, `guidance_events`,
      `snapshots`, `findings`, `confirmation_requests`, `artifacts`), with the
      `(run_id, sequence_number)` unique constraint and `workspace_id` as the
      cascade root; ordered runner invoked once at startup, no table creation in
      repository code (ADR-0003). Tier 2 tables stay out.
- [x] T2 — Connection configuration and the unit of work: WAL, foreign keys,
      5,000 ms busy timeout, `synchronous=FULL`, `BEGIN IMMEDIATE`, one owner
      per transaction; a lock timeout maps to `WORKSPACE_LOCK_TIMEOUT`,
      `retryable: true`, never a silent retry. Tests assert the pragmas.
- [x] T3 — Repository implementations of the core's `ContractRepository`,
      `SnapshotRepository`, `EventRepository`, and `FindingRepository`
      protocols, insert-only and append-only with no update or delete path;
      deterministic sequence allocation inside the appending transaction, with
      the unique constraint proven to be a backstop rather than the mechanism.
- [ ] T4 — Keyed per-workspace async lock manager with bounded cleanup;
      concurrent mutations in one workspace serialize, mutations in different
      workspaces proceed concurrently (FR-007), and nothing is held across a
      wait.
- [ ] T5 — Anonymous workspace middleware: cryptographically random ID, opaque
      `HttpOnly`, `SameSite=Strict` cookie, `Secure` in production only
      (FR-005); first access creates a workspace, and `last_seen_at` advances.
- [ ] T6 — Workspace authorization on every stateful endpoint resolved from the
      cookie (FR-006). Two-client tests prove a known identifier grants nothing:
      cross-workspace run, contract, confirmation, and artifact access all fail.
- [ ] T7 — `Origin` validation on mutations and the single §15.8 error envelope:
      core `CoreError` codes acquire an HTTP status here, invalid transitions
      return 409 using `journeys.transitions`, and no internal detail or
      traceback reaches a client.
- [ ] T8 — FR-008's hard ceilings, exact: 250 events per run with one slot
      reserved for the terminal `resource_limit_exceeded` carrying
      `EVENT_LIMIT_EXCEEDED`; 10 outcome runs, 10 eval cases, 20 eval runs,
      three suites of 100 trials, five pairings, 25 artifacts, 10 MiB of
      artifact bytes, two concurrent streams; over-cap creation returns
      `WORKSPACE_LIMIT_EXCEEDED` and commits nothing.
- [ ] T9 — FR-009's rate limits and garbage collection: 120 requests/minute with
      a burst of 30, 10 workspace creations/hour, keyed from the direct peer or
      explicitly trusted proxy metadata and never a client-supplied forwarding
      header; health checks and static assets excluded; stale-workspace and
      eval-workspace cleanup preserving built-in templates; stable 429 that
      never partially commits.
- [ ] T10 — Adapter registry: a missing or misconfigured optional target
      produces a bounded unavailable state, the service starts with Buggy Store
      disabled and reports the adapter unavailable, and one failed integration
      never disables the others (§21.1).
- [ ] T11 — `/api/v1/workspace` routes from §15.1: status, reset with optional
      `purge_completed`, failure-profile selection, and scenario-mode selection
      before arming. Reset cancels nonterminal work and unresolved confirmations
      while retaining terminal artifacts and the selected contract.
- [ ] T12 — Contract routes from §15.2: list built-in templates, read one
      immutable contract, and select the active contract, atomically selecting
      the target its `target_id` maps to (FR-024). The three templates are the
      ones `integrations.buggy_store` already seeds.
- [ ] T13 — Verify the full exit gate: two-client isolation across every
      workspace-owned resource, no partial state under resource/rate/lock
      failure, reset semantics, and a clean start with the Buggy Store disabled.
      Extend the architecture lane's exit-gate traceability map to 004.
