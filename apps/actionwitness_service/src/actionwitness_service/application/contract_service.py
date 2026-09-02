"""Built-in template seeding and contract selection (§15.2, FR-020, FR-023, FR-024).

Two rules do most of the work here.

**Templates are seeded, not authored.** The three Tier 1 contracts are the ones
`integrations.buggy_store` already ships (003-T12). They arrive as *data* from
the integration that understands what `target.cart.total` means, are validated
through the core's own `parse_contract` on the way in, and are stored with
`workspace_id IS NULL` so they belong to nobody. Re-authoring them here would
put commerce vocabulary in a target-neutral service, and copying them would give
the project two sources of truth for what a contract asserts.

Seeding is idempotent and keyed by content hash. A restart must not create a
second copy of the same template, and — more importantly — a template whose text
*changed* between releases must not silently overwrite the version an existing
run was armed against (FR-012: "completed evidence is never relabeled"). So a
changed template is stored as a new row with a new identity, and the old one
stays readable for the runs that used it.

**Selection is atomic with target selection.** FR-024: "Selecting it atomically
selects the server-registered target mapped from its immutable `target_id` ...
and no endpoint may combine a contract with a different target." One `UPDATE`
sets both columns, so there is no instant at which a workspace holds a contract
and the wrong target. If the target that the contract names is unavailable,
nothing is written at all and the refusal is `TARGET_UNAVAILABLE`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from actionwitness_core.contracts.models import ContractRecord, parse_contract
from actionwitness_core.security.canonical import content_hash

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import AdapterRegistry, TargetUnavailable
from actionwitness_service.application.authorization import WorkspaceScope
from actionwitness_service.application.guidance_service import (
    GuidanceRecorder,
    current_guidance,
)
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import ContractRepository, new_id

__all__ = ["ContractService", "seed_templates"]


async def seed_templates(work: UnitOfWork, templates: Iterable[Any]) -> int:
    """Store every built-in template that is not already stored.

    Returns how many were newly written, so startup can report it and a test can
    assert that a second run writes nothing.

    Identity is `template_id` plus the content hash of the document. That pairing
    is what makes the operation both idempotent and safe across a release: the
    same template seeds once, and an *edited* template becomes a second row
    rather than a silent rewrite of the one an armed run is pointing at.
    """
    repository = ContractRepository(work, None)
    # One instant for the whole batch. Stamping each row from its own clock read
    # gives three rows written in one transaction three different microsecond
    # timestamps, which makes `ORDER BY created_at, id` depend on how fast the
    # loop ran — a listing that is stable on one machine and shuffled on
    # another. They were seeded together, so they are stamped together.
    seeded_at = work.instant()
    written = 0
    for template in templates:
        document = dict(template.document)
        digest = content_hash(document)
        contract_id = f"tpl_{template.template_id}_{digest.removeprefix('sha256:')[:12]}"

        existing = await work.fetch_one(
            "SELECT id FROM contracts WHERE id = ? AND workspace_id IS NULL", (contract_id,)
        )
        if existing is not None:
            continue

        # Validated through the core on the way in, even though it is our own
        # data: constitution §4 requires persisted JSON to be validated on write,
        # and a template that stopped parsing after a model change should fail
        # at startup rather than at the first run armed against it.
        parse_contract(document)
        # The document is stored exactly as the template authored it. Its
        # provenance goes in a column: anything written *into* the document
        # would change the hash that is the contract's identity.
        await repository.add(
            ContractRecord(
                contract_id=contract_id,
                schema_version=str(document.get("schema_version", "1.0")),
                content_hash=digest,
                document=document,
                created_at=seeded_at,
            ),
            source_template_id=template.template_id,
        )
        written += 1
    return written


class ContractService:
    """§15.2's template list, contract read, and active-contract selection."""

    def __init__(self, work: UnitOfWork, workspace_id: str, registry: AdapterRegistry) -> None:
        self._work = work
        self._workspace_id = workspace_id
        self._registry = registry

    async def list_templates(self) -> list[Mapping[str, Any]]:
        """Every global built-in template (§15.2)."""
        rows = await ContractRepository(self._work).list_template_summaries()
        return [_summary(row) for row in rows]

    async def instantiate(
        self, document: Mapping[str, Any], *, source_template_id: str
    ) -> Mapping[str, Any]:
        """Persist one expanded template as an immutable contract (FR-021–023).

        The document arrives already expanded, because the arithmetic that
        produced it is target knowledge and this service is target-neutral. What
        happens here is the part that is the same for every target: validate,
        hash, store once, and never update.

        **Validation is not skipped because we generated it.** `parse_contract`
        runs on the expansion exactly as it runs on a seeded template — the
        constitution requires persisted JSON to be validated on write, and a
        template whose arithmetic produced an out-of-range total should fail
        here rather than at the first run armed against it.

        The contract is stored under this workspace rather than globally: it was
        created by one person's form submission, and FR-009 deletes a
        workspace's own contracts while preserving the built-in templates.
        """
        # Raises `ContractError`, which the boundary turns into §15.8's
        # envelope with field-level details — the same shape a rejected form
        # field produces, so a client parses one error format and not two.
        parse_contract(document)

        stored = dict(document)
        digest = content_hash(stored)
        record = ContractRecord(
            contract_id=new_id("ctr"),
            schema_version=str(stored.get("schema_version", "1.0")),
            content_hash=digest,
            document=stored,
            created_at=self._work.instant(),
        )
        await ContractRepository(self._work, self._workspace_id).add(
            record, source_template_id=source_template_id
        )
        return {
            "contract_id": record.contract_id,
            "content_hash": digest,
            "source_template_id": source_template_id,
            "name": str(stored.get("name", "")),
            "schema_version": record.schema_version,
            "document": stored,
        }

    async def read(self, contract_id: str) -> Mapping[str, Any]:
        """One immutable contract this workspace may see (§15.2, FR-006).

        The stored hash is re-derived and compared. A row whose document no
        longer matches its hash has been altered outside the insert-only path,
        and handing it back would let an edited contract decide a verdict
        (§17.2, constitution §5).
        """
        row = await WorkspaceScope(self._work, self._workspace_id).contract(contract_id)
        document = _loads(row["document_json"])
        if content_hash(document) != row["content_hash"]:
            # Deliberately `HARNESS_ERROR` rather than a new code. A stored
            # contract that no longer hashes to its recorded value was altered
            # outside the insert-only path, which is a fault in the deployment
            # and not something the caller can fix by changing their request.
            # Constitution §5: an integrity failure is an explicit non-pass, and
            # it never degrades into serving the document anyway.
            raise ApiError(
                ApiErrorCode.HARNESS_ERROR,
                "A stored contract failed its integrity check and was not served.",
            )
        return {
            "contract_id": row["id"],
            "schema_version": row["schema_version"],
            "content_hash": row["content_hash"],
            "source_template_id": row["source_template_id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "document": document,
            "is_built_in": row["workspace_id"] is None,
        }

    async def select(self, contract_id: str) -> Mapping[str, Any]:
        """FR-024: select the contract and, atomically, the target it names.

        The target comes from the contract's own immutable `target_id`, never
        from the request. That is the whole of "no endpoint may combine a
        contract with a different target" — there is no parameter to combine.
        """
        # FR-012: "Changing any value requires reset and creates a new run."
        # Selecting a different contract while a run is in flight would leave
        # the workspace pointing at one contract and the run judged by another,
        # which is the relabelling that requirement forbids.
        await _require_no_run_in_flight(self._work, self._workspace_id)

        contract = await self.read(contract_id)
        target_id = str(contract["document"].get("target_id", ""))

        slot = self._registry.resolve(target_id)
        if slot is None or not slot.is_available:
            # Nothing is written. FR-024's example is `shopify_exact_cart`
            # failing when the Shopify module is disabled, and a workspace left
            # holding a contract whose target cannot run would be exactly the
            # combination the requirement forbids.
            raise TargetUnavailable(
                target_id or "unknown",
                slot.state.reason if slot else "No adapter is registered for it.",
            )

        # One statement, both columns. There is no instant at which the
        # workspace holds this contract and a different target.
        await self._work.execute(
            "UPDATE workspaces SET selected_contract_id = ?, selected_target_id = ? WHERE id = ?",
            (contract_id, target_id, self._workspace_id),
        )

        # FR-122 / AC-21: the action history records each handoff. Selecting a
        # contract moves the workspace from `no_contract` to `contract_ready`
        # — a change of both phase and available action — and a history that
        # began at the first tool call would leave a reader unable to see when
        # the operator handed the journey to the agent. `transition` is a no-op
        # when the phase has not actually moved.
        await GuidanceRecorder(self._work, self._workspace_id).transition(
            await current_guidance(self._work, self._workspace_id)
        )
        return {"selected_contract_id": contract_id, "selected_target_id": target_id}


def _summary(row: Mapping[str, Any]) -> Mapping[str, Any]:
    """What a chooser needs, without the whole document.

    `description` and `target_id` come from the document because that is where
    the template author put them; `source_template_id` comes from the column
    because putting it in the document would change the contract's hash.
    """
    document = _loads(row["document_json"])
    return {
        "contract_id": row["id"],
        "source_template_id": row["source_template_id"],
        "name": row["name"],
        "description": document.get("description"),
        "target_id": document.get("target_id"),
        "schema_version": row["schema_version"],
        "content_hash": row["content_hash"],
    }


def _loads(raw: str) -> dict[str, Any]:
    import json

    parsed = json.loads(raw)
    if not isinstance(parsed, dict):  # pragma: no cover - the writer only stores objects
        raise ApiError(ApiErrorCode.HARNESS_ERROR, "A stored contract is not an object.")
    return parsed


async def _require_no_run_in_flight(work: UnitOfWork, workspace_id: str) -> None:
    """Refuse a selection change while a run holds the workspace (FR-012).

    `RUN_IN_PROGRESS` rather than FR-039's `RUN_MUTATION_LOCKED`: this is a
    change to the *harness's* run configuration, not a direct human mutation of
    target state, and conflating the two would tell a caller the wrong thing
    about what is locked and why.
    """
    from actionwitness_service.application.workspace_service import NONTERMINAL_RUN_STATES

    placeholders = ",".join("?" for _ in NONTERMINAL_RUN_STATES)
    row = await work.fetch_one(
        f"SELECT id FROM runs WHERE workspace_id = ? AND status IN ({placeholders}) LIMIT 1",
        (workspace_id, *sorted(NONTERMINAL_RUN_STATES)),
    )
    if row is not None:
        raise ApiError(
            ApiErrorCode.RUN_IN_PROGRESS,
            "A run is in progress; reset the workspace before selecting a different contract.",
        )
