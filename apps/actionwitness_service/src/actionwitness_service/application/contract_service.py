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

**Target-scoped validation runs at both moments, and refuses at neither when the
adapter is absent.** §10.2/§10.3 hold rules a contract cannot be judged against
on its own terms — every named tool must be one the selected adapter publishes,
and a protected mutation must carry a confirmation policy. Those rules need the
adapter's vocabulary, so they run here rather than in `parse_contract`, and they
run *twice* because instantiation and selection are different moments with
different adapter sets: a contract can be created while an integration is
enabled and selected after a restart that disabled it, or created before an
adapter's tool surface changed. Instantiation is where a person is standing in
front of the form and can be told what is wrong; selection is the moment the
contract is bound to the target a run will be judged against, and is therefore
the check that cannot be bypassed by a contract stored some other way.

Neither call fires when the named target resolves to no available adapter.
§21.1 requires the harness to run with an integration absent from the
environment entirely, and an absent adapter publishes no tools — validating
against it would turn "this target is not installed" into "every tool this
contract names is invented", which is a different and untrue statement. An
absent target stays what it already was: nothing at instantiation, and
`TARGET_UNAVAILABLE` at selection.

**One hashing rule.** §17.2 hashes "its validated contract document", which
`OutcomeContract.canonical_document()` is and a submitted document only might
be. Every stored contract is therefore built through `ContractRecord.of`, so the
document that is stored and the document that is hashed are the same document on
every path. Hashing the raw submission instead once produced contracts whose
stored hash disagreed with the hash §24.2 recomputes, and the failure surfaced
far away — at eval-case generation, as "the source contract does not match its
stored hash".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from actionwitness_core.contracts.models import (
    ContractRecord,
    OutcomeContract,
    parse_contract,
)
from actionwitness_core.ports.enums import SideEffectClass
from actionwitness_core.security.canonical import content_hash

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.adapter_registry import (
    AdapterRegistry,
    AdapterSlot,
    TargetUnavailable,
)
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
        # Validated through the core on the way in, even though it is our own
        # data: constitution §4 requires persisted JSON to be validated on write,
        # and a template that stopped parsing after a model change should fail
        # at startup rather than at the first run armed against it. It happens
        # before the already-seeded check rather than after, because the identity
        # below is derived from the *validated* document and there is nothing to
        # look up until it has been parsed. The cost is that every restart
        # re-parses every template, which is the right way round: a template that
        # stopped parsing should keep failing, not fail once and then be skipped.
        contract = parse_contract(document)
        digest = contract.content_hash()
        contract_id = f"tpl_{template.template_id}_{digest.removeprefix('sha256:')[:12]}"

        existing = await work.fetch_one(
            "SELECT id FROM contracts WHERE id = ? AND workspace_id IS NULL", (contract_id,)
        )
        if existing is not None:
            continue

        # A template is a *published* artifact: its text ships in the
        # integration, and the digest of that text is half of the row id above.
        # Storing a quietly normalised version would leave the shipped template
        # and the seeded contract as two different documents under an identity
        # derived from only one of them, so a template that is not already
        # written in its own canonical form fails loudly at startup instead.
        # `instantiate` below has no equivalent check because an expansion is
        # generated rather than published: there is no authored text for the
        # stored document to disagree with.
        if content_hash(document) != digest:
            raise ApiError(
                ApiErrorCode.HARNESS_ERROR,
                "A built-in contract template is not written in its canonical form.",
            )
        # Provenance goes in a column, never into the document: anything written
        # *into* the document would change the hash that is the contract's
        # identity.
        await repository.add(
            ContractRecord.of(contract, contract_id=contract_id, created_at=seeded_at),
            source_template_id=template.template_id,
        )
        written += 1
    return written


def _validate_against_target(contract: OutcomeContract, slot: AdapterSlot | None) -> None:
    """Apply §10.2/§10.3's target-scoped rules, or none if the target is absent.

    The core owns the rules and this owns the vocabulary, which is the split
    `OutcomeContract.validate_against_target` was written for: it takes plain
    names because the adapter registry is an application concern and a contract
    model that imported one would drag composition into the domain.

    Both lists come from the adapter's own published specs rather than from a
    constant here. §9.1 makes the adapter the authority on its own surface, and
    a harness that decided which tools sounded protected would be guessing about
    a target it is not allowed to know.

    An absent or unavailable adapter is a no-op and deliberately not a refusal.
    See the module docstring: an empty tool set would make every tool a contract
    names unpublished, and §21.1's "the integration is not installed" would
    arrive at the caller as "this contract is invalid".
    """
    if slot is None or slot.factory is None:
        return
    adapter = slot.factory()
    specs = adapter.tool_specs()
    contract.validate_against_target(
        target_id=adapter.descriptor.target_id,
        tool_names=[spec.name for spec in specs],
        protected_tools=[
            spec.name for spec in specs if spec.side_effect is SideEffectClass.PROTECTED_MUTATING
        ],
    )


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
        contract = parse_contract(document)

        # §10.2/§10.3, against the adapter this contract names. This is the
        # earlier of the two moments the rules can run, and the friendlier one:
        # the person is still at the form. It is not the enforcing one — see the
        # module docstring and `select`, which re-runs it against whatever
        # adapter set exists at the moment the contract is bound to a run.
        _validate_against_target(contract, self._registry.resolve(contract.target_id))

        # `ContractRecord.of` is the whole of the one hashing rule: the document
        # it stores and the document it hashes are both `canonical_document()`,
        # so this contract's identity is the same identity the core would give
        # it and the same one §24.2 recomputes when generating an eval case.
        record = ContractRecord.of(
            contract, contract_id=new_id("ctr"), created_at=self._work.instant()
        )
        await ContractRepository(self._work, self._workspace_id).add(
            record, source_template_id=source_template_id
        )
        stored = dict(record.document)
        return {
            "contract_id": record.contract_id,
            "content_hash": record.content_hash,
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

    async def stored_document(self, contract_id: str) -> str | None:
        """One contract's stored JSON text, by id alone, or `None`.

        Deliberately *not* workspace-scoped, and deliberately narrower than
        `read`: its caller is the replay path, which has already established
        that this is the id the workspace itself selected, and which needs the
        raw text rather than the response shape `read` builds. Kept here rather
        than inlined in the route so the one query that reaches a contract
        without a workspace term is visible in the service that owns contracts.
        """
        row = await self._work.fetch_one(
            "SELECT document_json FROM contracts WHERE id = ?", (contract_id,)
        )
        return None if row is None else str(row["document_json"])

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

        # §10.2/§10.3, against the adapter that was *just* resolved. This is the
        # enforcing call, and it is here rather than only at instantiation for
        # two reasons. Selection is the moment the contract stops being a
        # document and becomes the thing a run is judged against, so it is the
        # last point at which a mismatch can still be an honest refusal instead
        # of a `missing_expected_tool` finding blamed on the agent. And it is
        # the only point every contract passes through: a seeded template, a
        # contract created before an adapter's surface changed, and one created
        # while a different set of integrations was enabled all arrive here.
        #
        # The document was verified against its stored hash by `read` above, so
        # re-parsing it costs a validation of data the harness itself wrote — a
        # stored contract that no longer parses is a real refusal, not a
        # formality.
        _validate_against_target(parse_contract(contract["document"]), slot)

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
