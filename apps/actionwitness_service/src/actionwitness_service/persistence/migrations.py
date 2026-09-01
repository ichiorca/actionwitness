"""Explicit, ordered schema migrations for the harness database.

Spec v1.9 §17.1 (the table definitions, transcribed column by column), §17
(SQLite is the durable server-side source of truth); ADR-0003 ("schema is
created by an explicit, ordered, tested migration runner invoked once at
startup. No `CREATE TABLE IF NOT EXISTS` in repository code, and no placeholder
migration files"); constitution §4 ("schema changes use explicit, tested
migrations; startup-time table creation and placeholder migrations are
forbidden").

Only the nine Tier 1 tables BUILD_ORDER §7/M3 names are here. The Tier 2 tables
- evaluation cases and runs, benchmark suites and trials, Shopify pairings -
arrive in the M6/M7 migrations that first write to them. Shipping a schema no
code fills would make the migration list a wish rather than a record.

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
)

#: §17.1's Tier 2 eval tables, added by migration 2. Named separately from
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
