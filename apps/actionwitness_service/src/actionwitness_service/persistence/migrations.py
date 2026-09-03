"""Explicit, ordered schema migrations for the harness database.

Spec v1.9 §17.1 (the table definitions, transcribed column by column), §17
(SQLite is the durable server-side source of truth); ADR-0003 ("schema is
created by an explicit, ordered, tested migration runner invoked once at
startup. No `CREATE TABLE IF NOT EXISTS` in repository code, and no placeholder
migration files"); constitution §4 ("schema changes use explicit, tested
migrations; startup-time table creation and placeholder migrations are
forbidden").

Only the nine Tier 1 tables BUILD_ORDER §7/M3 names are here. The Tier 2 tables
- evaluation cases and runs, benchmark suites and trials - arrive in the M6/M7
migrations that first write to them, and Tier 3's `shopify_pairings` in
migration 9 alongside the service that fills it. Shipping a schema no code fills
would make the migration list a wish rather than a record.

Two structural rules run through every table:

**`workspace_id` is the cascade root.** The workspace is the isolation boundary
(constitution §2), so every workspace-owned row hangs off it by foreign key and
a workspace deletion takes its data with it. Cleanup then has one statement to
get right instead of nine.

**Evidence is append-only or insert-only.** `events` carries a unique
`(run_id, sequence_number)`; `snapshots`, `findings`, and `contracts` have no
update path at all. The repositories enforce that by having no such method
(§17.1: "the repository exposes no update method for this table"), and the
schema is the second line of defence rather than the only one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final, NamedTuple

import aiosqlite

__all__ = [
    "MIGRATIONS",
    "TIER_ONE_TABLES",
    "TIER_THREE_SHOPIFY_TABLES",
    "TIER_TWO_BENCHMARK_TABLES",
    "TIER_TWO_EVAL_TABLES",
    "Migration",
    "apply_migrations",
    "schema_version",
]


class Migration(NamedTuple):
    """One ordered, irreversible schema step."""

    version: int
    name: str
    statements: tuple[str, ...]


#: The nine tables BUILD_ORDER §7/M3 lists, for the gate that checks all nine
#: exist and that nothing from Tier 2 slipped in early.
TIER_ONE_TABLES: Final[tuple[str, ...]] = (
    "workspaces",
    "contracts",
    "runs",
    "events",
    "guidance_events",
    "snapshots",
    "findings",
    "confirmation_requests",
    "artifacts",
)


MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=1,
        name="tier one workspace, contract, run, evidence, and artifact tables",
        statements=(
            # §17.1 `workspaces`. `kind` is the core's `workspace_kind` enum;
            # `owner_workspace_id` is set only for internal eval workspaces and
            # points back at the interactive workspace that created them.
            """
            CREATE TABLE workspaces (
                id                        TEXT NOT NULL PRIMARY KEY,
                kind                      TEXT NOT NULL,
                owner_workspace_id        TEXT,
                selected_target_id        TEXT,
                selected_contract_id      TEXT,
                active_run_id             TEXT,
                active_evaluation_run_id  TEXT,
                active_benchmark_suite_id TEXT,
                active_shopify_pairing_id TEXT,
                failure_profile           TEXT,
                scenario_mode             TEXT,
                created_at                TEXT NOT NULL,
                last_seen_at              TEXT NOT NULL,
                cleaned_at                TEXT,
                FOREIGN KEY (owner_workspace_id)
                    REFERENCES workspaces (id) ON DELETE CASCADE
            )
            """,
            # Cleanup scans by inactivity, and the interactive/eval split
            # decides which rule applies (FR-009).
            """
            CREATE INDEX workspaces_by_activity ON workspaces (kind, last_seen_at)
            """,
            # §17.1 `contracts`. `workspace_id` is nullable because built-in
            # templates are global: FR-009's cleanup "preserv[es] global built-in
            # templates" precisely because they belong to no workspace.
            """
            CREATE TABLE contracts (
                id                 TEXT NOT NULL PRIMARY KEY,
                workspace_id       TEXT,
                source_template_id TEXT,
                content_hash       TEXT NOT NULL,
                name               TEXT NOT NULL,
                schema_version     TEXT NOT NULL,
                document_json      TEXT NOT NULL,
                created_at         TEXT NOT NULL,
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX contracts_by_workspace ON contracts (workspace_id)
            """,
            # §17.1 `runs`. Every controlled input is copied in at arming and
            # never updated afterwards (FR-012); only the lifecycle columns
            # (`status`, `completed_at`, `overall_result`, `comparison_key_hash`)
            # move, and §16 governs how.
            """
            CREATE TABLE runs (
                id                       TEXT NOT NULL PRIMARY KEY,
                workspace_id             TEXT NOT NULL,
                contract_id              TEXT,
                contract_content_hash    TEXT,
                target_id                TEXT NOT NULL,
                target_adapter_id        TEXT NOT NULL,
                scenario_mode            TEXT,
                failure_profile          TEXT,
                fault_active             INTEGER NOT NULL DEFAULT 0,
                fixture_content_hash     TEXT,
                intent_content_hash      TEXT,
                comparison_key_hash      TEXT,
                comparison_source_run_id TEXT,
                implementation_version   TEXT NOT NULL,
                build_commit             TEXT,
                status                   TEXT NOT NULL,
                started_at               TEXT NOT NULL,
                completed_at             TEXT,
                overall_result           TEXT,
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (contract_id) REFERENCES contracts (id),
                FOREIGN KEY (comparison_source_run_id) REFERENCES runs (id)
            )
            """,
            """
            CREATE INDEX runs_by_workspace ON runs (workspace_id, started_at)
            """,
            # §17.1 `events`, append-only. The unique constraint is the
            # correctness backstop for sequence allocation, not the allocation
            # mechanism (ADR-0003): if it ever fires, the transaction was wrong.
            """
            CREATE TABLE events (
                id                         TEXT NOT NULL PRIMARY KEY,
                run_id                     TEXT NOT NULL,
                sequence_number            INTEGER NOT NULL,
                event_type                 TEXT NOT NULL,
                actor                      TEXT NOT NULL,
                annotated_sequence_number  INTEGER,
                tool_identity_hash         TEXT,
                tool_name                  TEXT,
                correlation_id             TEXT,
                request_id                 TEXT,
                redacted_payload_json      TEXT NOT NULL,
                status                     TEXT,
                reported_status            TEXT,
                state_version_before       TEXT,
                state_version_after        TEXT,
                state_hash_before          TEXT,
                state_hash_after           TEXT,
                duration_ms                INTEGER,
                created_at                 TEXT NOT NULL,
                UNIQUE (run_id, sequence_number),
                FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
            )
            """,
            # Polling reads "events after sequence N" (§15.3), which is this
            # index read forwards.
            """
            CREATE INDEX events_by_run_sequence ON events (run_id, sequence_number)
            """,
            # §17.1 `guidance_events`, append-only and workspace-scoped because
            # guidance exists before a run does.
            """
            CREATE TABLE guidance_events (
                id                    TEXT NOT NULL PRIMARY KEY,
                workspace_id          TEXT NOT NULL,
                run_id                TEXT,
                workspace_version     INTEGER NOT NULL,
                phase                 TEXT NOT NULL,
                active_actor          TEXT NOT NULL,
                next_actor            TEXT,
                action_code           TEXT NOT NULL,
                copy_version          TEXT NOT NULL,
                instruction           TEXT NOT NULL,
                reason                TEXT NOT NULL,
                expected_consequence  TEXT NOT NULL,
                waiting_for           TEXT,
                recovery_action_code  TEXT,
                correlation_id        TEXT,
                resolution            TEXT,
                created_at            TEXT NOT NULL,
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX guidance_by_workspace
                ON guidance_events (workspace_id, workspace_version)
            """,
            # §17.1 `snapshots`. "Snapshot rows and payloads are insert-only.
            # The repository exposes no update method for this table."
            #
            # `namespace`, `provenance`, and `schema_version` are
            # project-allocated. §17.1 lists neither, but the core's
            # `SnapshotRepository.get` returns an `Observation`, and an
            # observation is not reconstructible without them: the namespace is
            # what an assertion path resolves through (§9.3), and dropping it
            # would make a restored snapshot answer a different contract than
            # the one it was captured for. Recorded in the 004 deviations
            # ledger. They sit beside the payload rather than inside it, for
            # the same reason §9.3 keeps `state_version` out — a key inside the
            # payload would be assertable and would change the content hash.
            """
            CREATE TABLE snapshots (
                id                  TEXT NOT NULL PRIMARY KEY,
                run_id              TEXT NOT NULL,
                phase               TEXT NOT NULL,
                provider            TEXT NOT NULL,
                namespace           TEXT NOT NULL,
                provenance          TEXT NOT NULL,
                schema_version      TEXT NOT NULL,
                state_version       TEXT,
                content_hash        TEXT NOT NULL,
                redacted_state_json TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                UNIQUE (run_id, phase),
                FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
            )
            """,
            # §17.1 `findings`. `path` holds a single-path finding and
            # `paths_json` a multi-path one; §17.1 forbids setting both.
            """
            CREATE TABLE findings (
                id                      TEXT NOT NULL PRIMARY KEY,
                run_id                  TEXT NOT NULL,
                check_id                TEXT NOT NULL,
                check_type              TEXT NOT NULL,
                classification          TEXT,
                severity                TEXT NOT NULL,
                status                  TEXT NOT NULL,
                path                    TEXT,
                paths_json              TEXT,
                applied_exemptions_json TEXT,
                attributed_cause_json   TEXT,
                expected_json           TEXT,
                actual_json             TEXT,
                evidence_json           TEXT,
                FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX findings_by_run ON findings (run_id)
            """,
            # §17.1 `confirmation_requests`. Bound to workspace *and* run, which
            # is what FR-066's single-use consumption revalidates against.
            """
            CREATE TABLE confirmation_requests (
                id                       TEXT NOT NULL PRIMARY KEY,
                workspace_id             TEXT NOT NULL,
                run_id                   TEXT NOT NULL,
                correlation_id           TEXT NOT NULL,
                tool_name                TEXT NOT NULL,
                state_binding_hash       TEXT NOT NULL,
                consequence_summary_json TEXT NOT NULL,
                status                   TEXT NOT NULL,
                expires_at               TEXT NOT NULL,
                decided_at               TEXT,
                consumed_at              TEXT,
                created_at               TEXT NOT NULL,
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX confirmations_by_workspace
                ON confirmation_requests (workspace_id, status)
            """,
            # §17.1 `artifacts`.
            #
            # `byte_size` is project-allocated: §17.1 lists no size column, but
            # FR-008 caps "10 MiB of persisted artifact bytes" per workspace, and
            # a cap enforced by stat-ing files during a transaction would be both
            # slow and racy. Recorded in the 004 deviations ledger.
            #
            # The Tier 2 owner columns are present because §17.1 lists them, but
            # they carry no foreign key: the tables they would reference do not
            # exist until M6/M7. Declaring a key to a missing table is an error;
            # declaring the column keeps the shape §17.1 fixed.
            """
            CREATE TABLE artifacts (
                id                  TEXT NOT NULL PRIMARY KEY,
                workspace_id        TEXT NOT NULL,
                run_id              TEXT,
                evaluation_case_id  TEXT,
                evaluation_run_id   TEXT,
                benchmark_suite_id  TEXT,
                shopify_pairing_id  TEXT,
                source_artifact_id  TEXT,
                artifact_type       TEXT NOT NULL,
                schema_version      TEXT NOT NULL,
                content_hash        TEXT NOT NULL,
                metadata_json       TEXT NOT NULL,
                relative_path       TEXT NOT NULL,
                byte_size           INTEGER NOT NULL DEFAULT 0,
                created_at          TEXT NOT NULL,
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE,
                FOREIGN KEY (source_artifact_id) REFERENCES artifacts (id)
            )
            """,
            """
            CREATE INDEX artifacts_by_workspace ON artifacts (workspace_id)
            """,
        ),
    ),
    Migration(
        version=2,
        name="tier two regression eval case, run, and event tables",
        statements=(
            # `source_run_id` carries NO foreign key, deliberately. FR-082 makes
            # a case portable: it is meant to be replayed from a clean checkout,
            # in a database that has never seen the run it was cut from. A key
            # to `runs` would assert the opposite, and the only ways to satisfy
            # it there are to fabricate a run row — manufacturing history the
            # harness never observed — or to refuse to record the case at all.
            # The column stays as recorded provenance; §24.1 keeps the case
            # self-contained, so nothing reads through it.
            #
            # §17.1 `evaluation_cases`. The unique constraint IS FR-080's
            # idempotence: "repeating `create_regression_eval` with the same
            # inputs returns the existing case and `created: false`; it never
            # mints a duplicate." Enforced by the database rather than by a
            # read-then-write, which two concurrent generators would both pass.
            """
            CREATE TABLE evaluation_cases (
                id                       TEXT NOT NULL PRIMARY KEY,
                workspace_id             TEXT NOT NULL,
                source_run_id            TEXT NOT NULL,
                contract_content_hash    TEXT NOT NULL,
                generator_schema_version TEXT NOT NULL,
                schema_version           TEXT NOT NULL,
                name                     TEXT NOT NULL,
                content_hash             TEXT NOT NULL,
                case_json                TEXT NOT NULL,
                created_at               TEXT NOT NULL,
                UNIQUE (source_run_id, contract_content_hash, generator_schema_version),
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX evaluation_cases_by_workspace
                ON evaluation_cases (workspace_id, created_at)
            """,
            # §17.1 `evaluation_runs`. Two workspaces, deliberately: the owner
            # is who asked, the execution workspace is the isolated `kind: eval`
            # one FR-083 requires, and collapsing them would let a replay mutate
            # the interactive workspace it was launched from.
            #
            # `status` and `overall_result` are separate columns for the same
            # reason they are separate report fields: §17.1 says status is the
            # expectation-matching status "while `overall_result` is the actual
            # evaluated business outcome", and a reproduced failure is `passed`
            # with `failed`.
            """
            CREATE TABLE evaluation_runs (
                id                            TEXT NOT NULL PRIMARY KEY,
                owner_workspace_id            TEXT NOT NULL,
                execution_workspace_id        TEXT NOT NULL,
                evaluation_case_id            TEXT NOT NULL,
                evaluation_case_content_hash  TEXT NOT NULL,
                mode                          TEXT NOT NULL,
                environment_profile           TEXT NOT NULL,
                implementation_version        TEXT NOT NULL,
                build_commit                  TEXT,
                status                        TEXT NOT NULL,
                overall_result                TEXT,
                started_at                    TEXT NOT NULL,
                completed_at                  TEXT,
                report_json                   TEXT,
                FOREIGN KEY (owner_workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (execution_workspace_id) REFERENCES workspaces (id),
                FOREIGN KEY (evaluation_case_id) REFERENCES evaluation_cases (id)
            )
            """,
            """
            CREATE INDEX evaluation_runs_by_owner
                ON evaluation_runs (owner_workspace_id, started_at)
            """,
            # §17.1 `evaluation_events`. A separate table from `events`, because
            # §16.1 is explicit that eval events "are append-only and belong
            # only to their `evaluation_run_id`; they never appear in the source
            # outcome run" — sharing one table would put a replay's evidence
            # inside the timeline it was cut from.
            """
            CREATE TABLE evaluation_events (
                id                     TEXT NOT NULL PRIMARY KEY,
                evaluation_run_id      TEXT NOT NULL,
                sequence_number        INTEGER NOT NULL,
                event_type             TEXT NOT NULL,
                actor                  TEXT NOT NULL,
                tool_name              TEXT,
                correlation_id         TEXT,
                request_id             TEXT,
                redacted_payload_json  TEXT NOT NULL,
                status                 TEXT,
                state_version_before   TEXT,
                state_version_after    TEXT,
                state_hash_before      TEXT,
                state_hash_after       TEXT,
                duration_ms            INTEGER,
                created_at             TEXT NOT NULL,
                UNIQUE (evaluation_run_id, sequence_number),
                FOREIGN KEY (evaluation_run_id) REFERENCES evaluation_runs (id) ON DELETE CASCADE
            )
            """,
            """
            CREATE INDEX evaluation_events_by_run
                ON evaluation_events (evaluation_run_id, sequence_number)
            """,
        ),
    ),
    Migration(
        version=3,
        name="tier two benchmark suite and trial tables",
        statements=(
            # §17.1 `benchmark_suites`. `source_kind` and `correlation_mode` are
            # columns on the *suite*, not on the trial alone, because FR-093
            # says "every suite contains exactly one source kind" and §9.9 says
            # the two correlation modes "shall never be aggregated into one
            # rate". Storing them here is what makes pooling a schema error
            # rather than a reporting mistake somebody has to notice.
            #
            # `result_artifact_id` is nullable until finalization and is set in
            # the same transaction that writes the artifact (§16.4: "either the
            # complete derived artifact and `result_artifact_id` are committed
            # together, or the suite enters `error` without a partial result").
            """
            CREATE TABLE benchmark_suites (
                id                        TEXT NOT NULL PRIMARY KEY,
                workspace_id              TEXT NOT NULL,
                schema_version            TEXT NOT NULL,
                source_kind               TEXT NOT NULL,
                manifest_content_hash     TEXT NOT NULL,
                manifest_json             TEXT NOT NULL,
                correlation_mode          TEXT NOT NULL,
                status                    TEXT NOT NULL,
                normalized_adapter_version TEXT NOT NULL,
                result_artifact_id        TEXT,
                created_at                TEXT NOT NULL,
                completed_at              TEXT,
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (result_artifact_id) REFERENCES artifacts (id)
            )
            """,
            """
            CREATE INDEX benchmark_suites_by_workspace
                ON benchmark_suites (workspace_id, created_at)
            """,
            # §17.1 `benchmark_trials`. The unique constraint is FR-091's
            # one-to-one guarantee at the storage layer: "a source run cannot be
            # counted twice in one benchmark". Enforced by the database rather
            # than by a read-then-write, which two concurrent binders would both
            # pass.
            #
            # `outcome_run_id` and `evaluation_run_id` carry no foreign keys for
            # the same reason `evaluation_cases.source_run_id` does not: an
            # imported report describes work done elsewhere, and a key would
            # assert that this database has the run it names. The service
            # validates a binding against rows that exist here before accepting
            # it, which is a check about *this* workspace rather than a
            # constraint that would make an imported artifact unstorable.
            """
            CREATE TABLE benchmark_trials (
                id                          TEXT NOT NULL PRIMARY KEY,
                benchmark_suite_id          TEXT NOT NULL,
                external_source_artifact_id TEXT NOT NULL,
                external_trial_id           TEXT NOT NULL,
                scenario_id                 TEXT NOT NULL,
                contract_content_hash       TEXT,
                scenario_mode               TEXT,
                failure_profile             TEXT,
                correlation_mode            TEXT NOT NULL,
                outcome_run_id              TEXT,
                evaluation_run_id           TEXT,
                call_level_result           TEXT NOT NULL,
                outcome_result              TEXT NOT NULL,
                eligibility                 TEXT NOT NULL,
                exclusion_reason            TEXT,
                metadata_json               TEXT NOT NULL,
                created_at                  TEXT NOT NULL,
                UNIQUE (benchmark_suite_id, external_trial_id),
                FOREIGN KEY (benchmark_suite_id)
                    REFERENCES benchmark_suites (id) ON DELETE CASCADE,
                FOREIGN KEY (external_source_artifact_id) REFERENCES artifacts (id)
            )
            """,
            # The partial uniqueness §17.1 asks for: "each non-null
            # `outcome_run_id` and `evaluation_run_id` within a suite". Partial
            # indexes rather than table constraints because SQLite treats NULLs
            # as distinct in a UNIQUE constraint, which would let every unbound
            # trial coexist — correct — but would also silently permit two
            # bound trials to share a run if the columns were nullable in one
            # constraint together.
            """
            CREATE UNIQUE INDEX benchmark_trials_one_outcome_run
                ON benchmark_trials (benchmark_suite_id, outcome_run_id)
                WHERE outcome_run_id IS NOT NULL
            """,
            """
            CREATE UNIQUE INDEX benchmark_trials_one_evaluation_run
                ON benchmark_trials (benchmark_suite_id, evaluation_run_id)
                WHERE evaluation_run_id IS NOT NULL
            """,
            """
            CREATE INDEX benchmark_trials_by_suite
                ON benchmark_trials (benchmark_suite_id, created_at)
            """,
        ),
    ),
    Migration(
        version=4,
        name="an evaluation run may originate from a benchmark trial",
        statements=(
            # §17.1 requires an eligible `imported_trajectory_replay` trial to
            # reference an evaluation run once it executes — but migration 2
            # shaped `evaluation_runs` around eval *cases* alone, with
            # `evaluation_case_id NOT NULL`. A benchmark replay has no case, and
            # both ways of satisfying the old shape were wrong: manufacturing a
            # case to carry the trial fabricates provenance the harness never
            # recorded, and leaving the trial unreferenced contradicts §17.1.
            #
            # The column is *widened*, never repurposed. Widening loses nothing
            # — every existing row is copied first and keeps its case id — and
            # the CHECK makes "exactly one origin" a schema fact rather than a
            # convention some later writer can forget. SQLite cannot relax NOT
            # NULL in place, so this is the documented table rebuild, running
            # inside the migration runner's single transaction: an interrupted
            # run rolls back rather than leaving the table half-copied.
            #
            # Operator-approved before it was written (008-T6).
            """
            CREATE TABLE evaluation_runs_rebuilt (
                id                            TEXT NOT NULL PRIMARY KEY,
                owner_workspace_id            TEXT NOT NULL,
                execution_workspace_id        TEXT NOT NULL,
                evaluation_case_id            TEXT,
                benchmark_trial_id            TEXT,
                evaluation_case_content_hash  TEXT NOT NULL,
                mode                          TEXT NOT NULL,
                environment_profile           TEXT NOT NULL,
                implementation_version        TEXT NOT NULL,
                build_commit                  TEXT,
                status                        TEXT NOT NULL,
                overall_result                TEXT,
                started_at                    TEXT NOT NULL,
                completed_at                  TEXT,
                report_json                   TEXT,
                CHECK (
                    (evaluation_case_id IS NOT NULL AND benchmark_trial_id IS NULL)
                    OR (evaluation_case_id IS NULL AND benchmark_trial_id IS NOT NULL)
                ),
                FOREIGN KEY (owner_workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (execution_workspace_id) REFERENCES workspaces (id),
                FOREIGN KEY (evaluation_case_id) REFERENCES evaluation_cases (id),
                FOREIGN KEY (benchmark_trial_id) REFERENCES benchmark_trials (id)
            )
            """,
            """
            INSERT INTO evaluation_runs_rebuilt (
                id, owner_workspace_id, execution_workspace_id, evaluation_case_id,
                benchmark_trial_id, evaluation_case_content_hash, mode,
                environment_profile, implementation_version, build_commit, status,
                overall_result, started_at, completed_at, report_json
            )
            SELECT
                id, owner_workspace_id, execution_workspace_id, evaluation_case_id,
                NULL, evaluation_case_content_hash, mode,
                environment_profile, implementation_version, build_commit, status,
                overall_result, started_at, completed_at, report_json
            FROM evaluation_runs
            """,
            "DROP TABLE evaluation_runs",
            "ALTER TABLE evaluation_runs_rebuilt RENAME TO evaluation_runs",
            """
            CREATE INDEX evaluation_runs_by_owner
                ON evaluation_runs (owner_workspace_id, started_at)
            """,
        ),
    ),
    Migration(
        version=5,
        name="authorized external-surface audits",
        statements=(
            # §22's `external_audits`. One row per authorized audit, and the row
            # cannot exist without the assertion that authorized it — FR-160:
            # "Absent authorization there is no audit."
            #
            # `authorized_origin` is immutable by convention *and* by absence:
            # nothing in the service updates it, because re-pointing an audit at
            # a second origin would let one assertion authorize a target the
            # operator never named.
            #
            # The status vocabulary is §22's, verbatim, and `expired` is in it
            # for a reason worth stating: "expiry never converts an incomplete
            # audit into a pass", so an audit that ran out of time is a terminal
            # state of its own rather than a completion.
            """
            CREATE TABLE external_audits (
                id                        TEXT NOT NULL PRIMARY KEY,
                workspace_id              TEXT NOT NULL,
                authorized_origin         TEXT NOT NULL,
                authorization_asserted_by TEXT NOT NULL,
                authorization_asserted_at TEXT NOT NULL,
                contract_pack_id          TEXT,
                run_id                    TEXT,
                surface_record_id         TEXT,
                status                    TEXT NOT NULL,
                report_artifact_id        TEXT,
                created_at                TEXT NOT NULL,
                completed_at              TEXT,
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE SET NULL,
                CHECK (status IN (
                    'authorized', 'paired', 'enumerated', 'pack_selected',
                    'running', 'completed', 'expired', 'cancelled', 'error'
                ))
            )
            """,
            # §22: "At most one nonterminal audit may exist per interactive
            # workspace." Enforced as a partial unique index rather than by a
            # read-then-write check, because two tabs asserting at once would
            # both read zero and both insert — and the second audit would be
            # pointed at an origin the first one's operator never saw.
            """
            CREATE UNIQUE INDEX external_audits_one_live_per_workspace
                ON external_audits (workspace_id)
             WHERE status NOT IN ('completed', 'expired', 'cancelled', 'error')
            """,
            """
            CREATE INDEX external_audits_by_workspace
                ON external_audits (workspace_id, created_at DESC)
            """,
        ),
    ),
    Migration(
        version=6,
        name="a confirmation records the arguments it was asked about",
        statements=(
            # Constitution §5 binds a confirmation to "the workspace, run,
            # action, arguments, and expiry". Migration 1 recorded four of those
            # five. `state_binding_hash` is not the fifth and was never meant to
            # be: `binding_hash` hashes the independently observed state and
            # deliberately nothing else, so an approval shown for one set of
            # arguments authorized every other set that left the observed world
            # unchanged — one person's consent replayed onto an action they were
            # never shown.
            #
            # Additive, and nullable for exactly one reason. A database written
            # before this migration holds approvals whose arguments were never
            # recorded, and backfilling a value would invent the very binding the
            # column exists to prove. Those rows keep `NULL`, and the resume path
            # refuses them: an unbound approval fails closed rather than being
            # trusted, because §5 makes an ambiguity an explicit non-pass and
            # never a degradation to success.
            """
            ALTER TABLE confirmation_requests ADD COLUMN arguments_hash TEXT
            """,
        ),
    ),
    Migration(
        version=7,
        name="a self-witnessing run records the workspace it observes",
        statements=(
            # FR-172: "a self-witnessing run shall observe a workspace other
            # than the one recording it." This column is where the *other* one is
            # named, on the recording workspace's row beside the target and
            # contract it already selects.
            #
            # It is deliberately not a foreign key back into `workspaces`.
            # SQLite cannot add a foreign key with `ALTER TABLE ADD COLUMN`, and
            # rebuilding the root table of nine cascades to gain one is a
            # destructive migration bought for a constraint the code already
            # holds: the value is only ever written by
            # `create_observed_workspace`, which inserts the row it then names,
            # inside the same transaction. The cascade that matters runs the
            # other way and already exists — the observed workspace's
            # `owner_workspace_id` points here, so deleting a recording
            # workspace takes the workspace it observed with it.
            #
            # Nullable, and null is the ordinary case: every workspace that is
            # not running a self-witnessing run observes nothing, which is a
            # different statement from observing itself and must stay
            # distinguishable from it.
            """
            ALTER TABLE workspaces ADD COLUMN observed_workspace_id TEXT
            """,
        ),
    ),
    Migration(
        version=8,
        name="a benchmark trial records which repetition of which variant it is",
        statements=(
            # §26.5's Tier 3 showcase is "six intent variants with five repeated
            # trials each", and FR-100 freezes those variants "before trials
            # begin; generation is not rerun between repetitions". Migration 3
            # gave a trial no way to say which variant it exercised or which
            # repetition it was, so five repeats of one variant were
            # indistinguishable from five unrelated trials — and a rate computed
            # over them would have been a rate over a population nobody defined.
            #
            # Additive and nullable, because null is the ordinary case and means
            # something specific: a trial imported from an evaluator report is
            # not a repetition of anything this harness ran, and backfilling it
            # with `repetition_index = 1` would invent a repeated trial that
            # never happened. The correlation view reads a null variant as "group
            # this trial by its scenario instead", which is what §24.7 step 1
            # already calls the shared intent.
            #
            # `variant_index` is a position into the frozen set the manifest
            # carries, not a foreign key: FR-100 seals that set into
            # `manifest_json`, and the whole point of freezing is that the set
            # cannot move underneath the index. Storing the variant *text* here
            # instead would be a second copy of a hashed document, free to drift
            # from the one the manifest hash covers.
            """
            ALTER TABLE benchmark_trials ADD COLUMN variant_index INTEGER
            """,
            """
            ALTER TABLE benchmark_trials ADD COLUMN repetition_index INTEGER
            """,
            # The trial whose evaluator verdict and recorded trajectory this
            # repetition re-executes. Recorded rather than inferred, because the
            # call-level half of a repetition is *not* a new measurement — it is
            # the same imported self-report, and a reader has to be able to see
            # which one. Without this column a repetition would look like an
            # independent evaluator observation, which is exactly the promotion
            # of a self-report the constitution forbids.
            """
            ALTER TABLE benchmark_trials ADD COLUMN source_external_trial_id TEXT
            """,
        ),
    ),
    Migration(
        version=9,
        name="tier three shopify development-store pairings",
        statements=(
            # §17.1 `shopify_pairings`, transcribed column by column, and §16.5's
            # ten states as a CHECK so a status outside the machine is a schema
            # error rather than a row nobody can classify.
            #
            # **Only hashes.** FR-111: "Only its hash is persisted." There is no
            # column here that could hold a raw credential, which is the point —
            # a redaction rule can be forgotten, an absent column cannot. The two
            # hash columns are the whole of what this table knows about the two
            # credentials, and `bridge_session_token_hash` is nullable because it
            # does not exist until redemption.
            #
            # `contract_id` and `contract_content_hash` are both recorded for the
            # reason `runs` records both: the id says which contract, the hash
            # says which *version* of it, and FR-025 judges a trial against the
            # document it was paired with even if the workspace later selects
            # another.
            #
            # `run_id` is nullable "until the `before` observation is accepted"
            # (§17.1) and carries `ON DELETE SET NULL` rather than a cascade:
            # §15.1's opt-in purge deletes terminal runs, and a purge must not
            # silently delete the pairing record that says a trial happened.
            """
            CREATE TABLE shopify_pairings (
                id                        TEXT NOT NULL PRIMARY KEY,
                workspace_id              TEXT NOT NULL,
                contract_id               TEXT NOT NULL,
                contract_content_hash     TEXT NOT NULL,
                run_id                    TEXT,
                store_origin              TEXT NOT NULL,
                pairing_token_hash        TEXT NOT NULL,
                bridge_session_token_hash TEXT,
                bridge_version            TEXT,
                theme_build_id            TEXT,
                status                    TEXT NOT NULL,
                expires_at                TEXT NOT NULL,
                redeemed_at               TEXT,
                completed_at              TEXT,
                created_at                TEXT NOT NULL,
                FOREIGN KEY (workspace_id) REFERENCES workspaces (id) ON DELETE CASCADE,
                FOREIGN KEY (contract_id) REFERENCES contracts (id),
                FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE SET NULL,
                CHECK (status IN (
                    'created', 'paired', 'armed', 'verifying', 'passed',
                    'passed_with_warnings', 'failed', 'expired', 'cancelled', 'error'
                ))
            )
            """,
            # §17.1: "At most one nonterminal pairing may exist per interactive
            # workspace." A partial unique index rather than a read-then-write
            # check, for the same reason the audit slot is one: two tabs starting
            # a pairing at once would both read zero and both insert, and the
            # second bridge would then redeem against a pairing whose operator
            # never saw the launch URL.
            #
            # The terminal set is §16.5's, written out rather than derived. A
            # SQLite index expression cannot import an enum, so the list has to be
            # literal here; `TERMINAL_PAIRING_STATUSES` in the service is the same
            # set, and a test compares the two so they cannot drift.
            """
            CREATE UNIQUE INDEX shopify_pairings_one_live_per_workspace
                ON shopify_pairings (workspace_id)
             WHERE status NOT IN (
                 'passed', 'passed_with_warnings', 'failed', 'expired', 'cancelled', 'error'
             )
            """,
            # §17.1: "`run_id` becomes unique when populated." Partial, because
            # SQLite treats NULLs as distinct in a plain UNIQUE — which is the
            # behaviour wanted for the many pairings that never armed a run, and
            # the wrong one for two pairings claiming the same run.
            """
            CREATE UNIQUE INDEX shopify_pairings_one_run
                ON shopify_pairings (run_id)
             WHERE run_id IS NOT NULL
            """,
            """
            CREATE INDEX shopify_pairings_by_workspace
                ON shopify_pairings (workspace_id, created_at DESC)
            """,
        ),
    ),
    Migration(
        version=10,
        name="shopify external capture path provenance",
        statements=(
            """
            ALTER TABLE shopify_pairings ADD COLUMN before_capture_path TEXT
                CHECK (
                    before_capture_path IS NULL OR (
                        length(before_capture_path) BETWEEN 1 AND 2048
                        AND substr(before_capture_path, 1, 1) = '/'
                        AND instr(before_capture_path, '?') = 0
                        AND instr(before_capture_path, '#') = 0
                    )
                )
            """,
            """
            ALTER TABLE shopify_pairings ADD COLUMN after_capture_path TEXT
                CHECK (
                    after_capture_path IS NULL OR (
                        length(after_capture_path) BETWEEN 1 AND 2048
                        AND substr(after_capture_path, 1, 1) = '/'
                        AND instr(after_capture_path, '?') = 0
                        AND instr(after_capture_path, '#') = 0
                    )
                )
            """,
        ),
    ),
)

#: §17.1's Tier 2 eval tables, added by migration 2. Migration 4 rebuilt
#: `evaluation_runs` to widen one column; the table is still migration 2's. Named separately from
#: `TIER_ONE_TABLES` so the M3 gate keeps asserting that nothing from Tier 2
#: slipped into migration 1 — the check that would otherwise quietly weaken the
#: moment this milestone landed.
TIER_TWO_EVAL_TABLES: Final[tuple[str, ...]] = (
    "evaluation_cases",
    "evaluation_runs",
    "evaluation_events",
)

#: §17.1's Tier 2 benchmark tables, added by migration 3. Separate from the eval
#: tuple above for the same reason that one is separate from Tier 1: the M3 gate
#: asserts each milestone's tables arrived in its own migration, and one merged
#: tuple could not tell migration 2's tables from migration 3's.
TIER_TWO_BENCHMARK_TABLES: Final[tuple[str, ...]] = (
    "benchmark_suites",
    "benchmark_trials",
)

#: §17.1's Tier 3 table, added by migration 9 alongside `ShopifyPairingService`
#: — the code that fills it. Declared here for the same reason the two tuples
#: above are: the migration gate asserts that every table the schema creates is
#: named by one of these tuples, so a table added without a declaration fails
#: rather than arriving unannounced.
TIER_THREE_SHOPIFY_TABLES: Final[tuple[str, ...]] = ("shopify_pairings",)


async def schema_version(connection: aiosqlite.Connection) -> int:
    """The highest migration this database has applied.

    `user_version` is a SQLite header field, so it needs no table of its own and
    cannot itself be the thing a missing migration failed to create.
    """
    async with connection.execute("PRAGMA user_version") as cursor:
        row = await cursor.fetchone()
    return int(row[0]) if row else 0


async def apply_migrations(
    connection: aiosqlite.Connection, migrations: Sequence[Migration] = MIGRATIONS
) -> int:
    """Apply every migration this database has not yet run, in order.

    Each migration and its version bump commit together, so an interrupted run
    leaves the database at a version whose schema actually exists. Returns the
    resulting schema version.
    """
    current = await schema_version(connection)
    for migration in migrations:
        if migration.version <= current:
            continue
        if migration.version != current + 1:
            raise RuntimeError(
                f"migration {migration.version} ({migration.name}) cannot follow schema "
                f"version {current}; migrations are ordered and contiguous"
            )
        await connection.execute("BEGIN IMMEDIATE")
        try:
            for statement in migration.statements:
                await connection.execute(statement)
            # PRAGMA takes no parameter binding, and `version` is an int from a
            # module-level literal rather than anything a caller supplies.
            await connection.execute(f"PRAGMA user_version = {int(migration.version)}")
        except Exception:
            await connection.rollback()
            raise
        await connection.commit()
        current = migration.version
    return current
