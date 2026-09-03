"""Benchmark routes (spec v1.9 §15.6, FR-090–FR-094).

| Method | Endpoint                                | Purpose |
|--------|-----------------------------------------|---------|
| `POST` | `/benchmarks`                           | Create a suite from a validated manifest |
| `POST` | `/benchmarks/{id}/imports`              | Import, validate, redact, preserve, normalize |
| `POST` | `/benchmarks/{id}/intent-variants`      | Draft candidate variants, unapproved (FR-100) |
| `POST` | `/benchmarks/{id}/frozen-variants`      | Seal the approved intent variants (FR-100) |
| `PUT`  | `/benchmarks/{id}/bindings`             | Save explicit one-to-one trial bindings |
| `POST` | `/benchmarks/{id}/replay`               | Execute eligible replay trials in isolation |
| `POST` | `/benchmarks/{id}/repeated-trials`      | Run one frozen variant again, N times (§26.5) |
| `GET`  | `/benchmarks/{id}/correlation`          | Per-variant evaluator-vs-observed correlation |
| `POST` | `/benchmarks/{id}/finalize`             | Create the immutable derived artifact |
| `GET`  | `/benchmarks/{id}`                      | Status, metadata, matrix, metrics, trials |
| `GET`  | `/benchmarks/{id}/trials/{trial_id}`    | Bounded redacted evidence for one trial |
| `GET`  | `/benchmarks/{id}/report`               | Download the immutable benchmark report |

**The import body is bytes, not parsed JSON.** FR-090 caps the artifact at 1 MiB
and BUILD_ORDER §7/M7 says "before parsing". A FastAPI model parameter would
have parsed the document before any handler ran, spending exactly the cost the
cap exists to prevent — so the raw body is read and handed to the reader, which
measures first.

**`/report` returns the stored bytes.** A benchmark is identified by its content
hash, and a reader who downloads one must be able to recompute that hash and get
the same answer. Re-serialising here would produce a document that is equal but
not identical, and the hash would not match.

**Bindings are `PUT` and the suite must still be `draft`.** §16.4 freezes them at
`ready`; the service refuses afterwards, and this layer does not soften it.

**Freezing variants is `POST` to a sub-resource, not `PUT` to the manifest.**
FR-100 forbids rerunning generation between repetitions, so the frozen set is
created once and never replaced — and `PUT` would advertise the idempotent
overwrite the requirement exists to prevent.

**Generation and freezing are two endpoints, not one.** `/intent-variants`
drafts candidates and stores nothing; `/frozen-variants` records a named
person's decision. Collapsing them would let one request both write the
variants and approve them, which is the consent an agent may not create for
itself — and it would also make FR-100's "generation is not rerun between
repetitions" unenforceable, because the freeze could no longer be the thing that
happens once.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

import httpx
from actionwitness_core.benchmarks.approval import approve
from actionwitness_core.benchmarks.enums import CorrelationMode, SourceKind, VariantKind
from actionwitness_core.benchmarks.intents import (
    MAX_INTENT_VARIANT_CHARS,
    MAX_INTENT_VARIANTS,
    validate_candidates,
)
from actionwitness_core.benchmarks.models import ScenarioDefinition, TrialBinding
from actionwitness_core.benchmarks.screening import require_screened
from actionwitness_core.kernel import CoreError
from fastapi import APIRouter, Body, Path, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from actionwitness_service.api.dependencies import (
    ArtifactsDependency,
    DatabaseDependency,
    LocksDependency,
    RegistryDependency,
    SettingsDependency,
    WorkspaceDependency,
)
from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.benchmark_service import (
    MAX_TRIAL_REPETITIONS,
    BenchmarkService,
    write_benchmark_report,
)
from actionwitness_service.application.variant_generation import (
    MAX_GENERATED_VARIANTS,
    CredentialUnavailable,
    VariantGenerationService,
    VariantProposer,
    live_credential,
)
from actionwitness_service.config import LiveEvaluatorSettings

__all__ = ["router"]

router = APIRouter(tags=["benchmarks"])

BenchmarkId = Annotated[str, Path(min_length=1, max_length=128)]
TrialId = Annotated[str, Path(min_length=1, max_length=128)]


class _Body(BaseModel):
    """Closed request bodies: an unknown field is a rejection, not a shrug."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ScenarioRequest(_Body):
    """§24.7 step 1: the target configuration one scenario runs under.

    Declared by the benchmark rather than read from the evaluator report, which
    describes what a model called and not what it called it against.
    """

    scenario_id: Annotated[str, Field(min_length=1, max_length=128)]
    scenario_mode: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    failure_profile: Annotated[str, Field(min_length=1, max_length=64)] | None = None


class CreateBenchmarkRequest(_Body):
    """§15.6: "create a benchmark suite from a validated manifest"."""

    source_kind: SourceKind = SourceKind.RECORDED_FIXTURE
    correlation_mode: CorrelationMode = CorrelationMode.IMPORTED_TRAJECTORY_REPLAY
    scenarios: tuple[ScenarioRequest, ...] = ()


class BindingRequest(_Body):
    """One explicit one-to-one binding (FR-091)."""

    external_trial_id: Annotated[str, Field(min_length=1, max_length=128)]
    outcome_run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    evaluation_run_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    #: FR-091's "explicit one-to-one developer choice" for a trial whose
    #: `(test.name, runIndex)` was absent or duplicated. Named in the request
    #: rather than inferred, because the whole point is that a human decided.
    acknowledge_unaddressable: bool = False


class BindingsRequest(_Body):
    bindings: tuple[BindingRequest, ...] = ()
    #: §16.4's `draft` → `ready`. Optional so a caller can save bindings in
    #: several calls and seal once, rather than being forced to send them all
    #: at once or to seal prematurely.
    seal: bool = False


class VariantRequest(_Body):
    """One candidate variant, exactly as the reviewer read it (FR-100)."""

    kind: VariantKind
    #: Bounded here with the core's own constant rather than a second number.
    #: The boundary caps the bytes; the core owns the *reason* a variant is too
    #: short to be a paraphrase of anything, and says so in its refusal.
    text: Annotated[str, Field(min_length=1, max_length=MAX_INTENT_VARIANT_CHARS)]


class FreezeVariantsRequest(_Body):
    """FR-100's last clause: the set a human approved, and who approved it.

    **There is no `actor` field, and that is the point.** The constitution
    forbids an agent creating or approving its own consent, so the actor is not
    something a request may state — it is fixed to `human` on the way into the
    core, which refuses any other value. A field here would be an invitation to
    record an approval nobody made.

    **The whole reviewed set arrives, not just the approved part.** An approval
    is a statement about specific texts, and the core binds it to a fingerprint
    of the full set; sending only the survivors would produce a record that
    could not say what was turned down.
    """

    canonical_intent: Annotated[str, Field(min_length=1, max_length=MAX_INTENT_VARIANT_CHARS)]
    variants: Annotated[tuple[VariantRequest, ...], Field(max_length=MAX_INTENT_VARIANTS)] = ()
    #: Positions within `variants`. Explicit rather than "all": a reviewer who
    #: rejected two of six made a decision the record has to carry.
    approved_indices: tuple[Annotated[int, Field(ge=0)], ...] = ()
    reviewer: Annotated[str, Field(min_length=1, max_length=128)]
    note: Annotated[str, Field(max_length=500)] = ""


class GenerateVariantsRequest(_Body):
    """FR-100's generate step: one canonical intent, and how many drafts.

    **There is no `reviewer` field and no `approved_indices` field.** Generation
    produces candidates and nothing else; an approval is a separate request a
    person makes after reading them. A body that could carry both would let one
    call write the variants and approve them — the consent an agent may not
    create for itself.

    **There is no model, provider, or endpoint field either.** Which backend
    this deployment talks to is server-controlled configuration (§20.1); a
    caller who could name one could point the harness at an arbitrary origin.
    """

    canonical_intent: Annotated[str, Field(min_length=1, max_length=MAX_INTENT_VARIANT_CHARS)]
    #: Bounded by FR-100's ceiling at the boundary as well as in the core. A
    #: request for seven is refused rather than clamped: clamping would answer a
    #: question the caller did not ask.
    count: Annotated[int, Field(ge=1, le=MAX_GENERATED_VARIANTS)] = 3


_DEFAULT_CREATE = CreateBenchmarkRequest()


@router.post("/benchmarks", status_code=201)
async def create_benchmark(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    settings: SettingsDependency,
    request: Annotated[CreateBenchmarkRequest, Body()] = _DEFAULT_CREATE,
) -> dict[str, Any]:
    """§15.6: a new suite, in `draft`.

    **A client cannot claim a live run.** AC-17 requires the *application* to
    label a live suite `live_model_run`, and §25.3 requires a checked-in report
    never to be "presented as a live execution" — so `live_model_run` is
    accepted only where a live backend is actually configured.

    Refused rather than quietly downgraded. A caller who asked for a live run
    and silently received a fixture-labelled suite would go on to present its
    numbers as a model result, which is the precise misrepresentation the two
    requirements exist to prevent.
    """
    from integrations.google_evals.live import source_kind_for

    if request.source_kind is SourceKind.LIVE_MODEL_RUN:
        available = source_kind_for(settings.live_evaluator)
        if available is not SourceKind.LIVE_MODEL_RUN:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This deployment has no configured live model backend, so a suite "
                "cannot be labelled `live_model_run`. Import the checked-in report "
                "as `recorded_fixture` instead — it produces the same matrix and "
                "says truthfully where it came from.",
            )

    async with locks.hold(workspace_id), database.transaction() as work:
        benchmark_id = await BenchmarkService(work, workspace_id).create(
            source_kind=request.source_kind,
            correlation_mode=request.correlation_mode,
            scenarios=tuple(
                ScenarioDefinition(
                    scenario_id=scenario.scenario_id,
                    scenario_mode=scenario.scenario_mode,
                    failure_profile=scenario.failure_profile,
                )
                for scenario in request.scenarios
            ),
        )
    return {
        "benchmark_id": benchmark_id,
        "status": "draft",
        "source_kind": request.source_kind.value,
        "correlation_mode": request.correlation_mode.value,
    }


@router.post("/benchmarks/{benchmark_id}/imports", status_code=201)
async def import_evaluator_report(
    benchmark_id: BenchmarkId,
    http_request: Request,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """§15.6: "import, validate, redact, preserve, and normalize".

    In that order, and the order is the control (FR-090). The raw body is read
    as bytes so the size cap precedes parsing; the redacted document is what is
    hashed and preserved as the immutable source artifact; normalization runs
    last, over a document that has already been validated and redacted.
    """
    from integrations.google_evals.live import (
        CredentialMaterialRejected,
        screen_for_credential_material,
    )
    from integrations.google_evals.normalize import normalize
    from integrations.google_evals.reader import ImportLimits, ReportRejected, read_report

    limits = settings.evaluator_import
    if limits is None:
        raise ApiError(
            ApiErrorCode.TARGET_UNAVAILABLE,
            "Evaluator import is disabled in this deployment.",
        )

    raw = await http_request.body()
    try:
        imported = read_report(
            raw,
            limits=ImportLimits(max_bytes=limits.max_report_bytes, max_trials=limits.max_trials),
        )
    except ReportRejected as rejected:
        raise _rejection(rejected) from rejected

    # FR-099: a credential must never arrive through an uploaded manifest.
    # Screened *before* anything is written, because a secret in a persisted
    # artifact is an incident rather than a validation failure — and because
    # refusing the whole import is what makes the value's existence visible
    # instead of quietly redacted away.
    try:
        screen_for_credential_material(imported.document, settings.live_evaluator)
    except CredentialMaterialRejected as carried:
        raise ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(carried)) from carried

    # The workspace lock is held across both transactions, so the suite cannot
    # move between the refusal check and the recording. What is deliberately
    # *not* held across the file write is the database transaction: ADR-0003
    # forbids it, and `BEGIN IMMEDIATE` is SQLite's single writer for every
    # workspace, not just this one.
    async with locks.hold(workspace_id):
        async with database.reading() as work:
            prepared = await BenchmarkService(work, workspace_id).prepare_import(benchmark_id)
        normalized = normalize(imported, correlation_mode=prepared.correlation_mode)

        # The *redacted* document is the artifact, hashed as stored.
        written = artifacts.write(
            workspace_id,
            benchmark_id,
            dict(imported.document),
            artifact_type="evaluator_report",
            schema_version=imported.reporter_schema,
        )
        async with database.transaction() as work:
            service = BenchmarkService(work, workspace_id)
            source_artifact_id = await artifacts.record(
                work,
                workspace_id,
                None,
                written,
                # FR-101: the live evaluator report is persisted as an immutable
                # benchmark *source*. The source kind travels with it so the
                # artifact can be identified later without consulting the suite —
                # a suite can be deleted with its workspace, and an artifact that
                # could not say what kind of run produced it would be unusable
                # evidence.
                metadata={
                    "reporter_schema": imported.reporter_schema,
                    "redacted": imported.redacted,
                    "source_kind": prepared.source_kind,
                },
                benchmark_suite_id=benchmark_id,
            )
            await service.record_import(
                benchmark_id,
                source_artifact_id=source_artifact_id,
                trials=normalized.trials,
                manifest_fields=normalized.manifest_fields,
            )

    return {
        "benchmark_id": benchmark_id,
        "source_artifact_id": source_artifact_id,
        "content_hash": imported.content_hash,
        "reporter_schema": imported.reporter_schema,
        "normalized_adapter_version": imported.normalizer_version,
        "trial_count": imported.trial_count,
        # FR-091: these need an explicit human choice before they can bind.
        "unaddressable_trial_ids": list(normalized.unaddressable_trial_ids),
    }


@asynccontextmanager
async def _live_proposer(
    http_request: Request, live: LiveEvaluatorSettings
) -> AsyncIterator[VariantProposer]:
    """The configured live model, wired to an HTTP client with a bounded life.

    **The client is injected into the proposer, never built inside it**
    (ADR-0001). What this scope owns is the *wiring*: a deployment that
    supplied its own client on `app.state.live_variant_client` keeps it — the
    same rule the lifespan applies to the target client, and the seam a test
    uses to hand in an `httpx.MockTransport` so no test ever reaches Google.

    **Request-scoped rather than lifespan-owned, deliberately.** ADR-0001's
    reason for one long-lived client is connection reuse on the run path, where
    an observation happens on every invocation. This call happens once while a
    person is drafting a variant set, so a per-request client costs one
    handshake and buys deterministic closure — and closure is what matters here,
    because this is the only client in the process that has ever held a
    credential.

    **The credential is read here and passed straight through.** Not resolved at
    startup, not stored in `ServiceSettings`, not put on `app.state`: it exists
    for the duration of one request, inside one object whose `repr` does not
    show it.
    """
    from integrations.google_evals.generation import LiveVariantClient, as_candidates
    from integrations.google_evals.live import describe_live_run

    # `os.environ` in a deployment; `live_environ` is the injected mapping a
    # test composes with, for the same reason `config.py` resolves settings from
    # one — every absence and misconfiguration stays testable without mutating
    # process state. Read here rather than at startup so the value is never held
    # anywhere between requests.
    environ: Mapping[str, str] = getattr(http_request.app.state, "live_environ", os.environ)
    supplied: httpx.AsyncClient | None = getattr(
        http_request.app.state, "live_variant_client", None
    )
    client = supplied or httpx.AsyncClient()
    try:
        model = LiveVariantClient(
            client=client,
            configuration=describe_live_run(live),
            credential=live_credential(live.credential_var, environ),
        )

        class _Proposer:
            """Adapts the client's typed variants to the loose mappings the
            service validates. Declared here rather than in the integration
            because the shape is the *service's* contract, not the vendor's."""

            async def propose(
                self, canonical_intent: str, *, count: int = 3
            ) -> list[dict[str, str]]:
                return as_candidates(await model.propose(canonical_intent, count=count))

        yield _Proposer()
    finally:
        if supplied is None:
            await client.aclose()


@router.post("/benchmarks/{benchmark_id}/intent-variants")
async def generate_benchmark_variants(
    benchmark_id: BenchmarkId,
    http_request: Request,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    settings: SettingsDependency,
    request: Annotated[GenerateVariantsRequest, Body()],
) -> dict[str, Any]:
    """FR-100's generate step: candidates for a human to read, and nothing more.

    **Nothing is persisted and nothing is approved.** The response is a draft a
    reviewer edits, ticks, and then submits to `/frozen-variants`, which is
    still the only place an approval is recorded and the only place a variant
    reaches a manifest. A route that wrote what it generated would freeze
    material nobody read.

    **The suite is checked first, and only while it is `draft`.** Generating for
    a suite that has already frozen a set, or that has left `draft`, would offer
    a person work the freeze route must then refuse — and FR-100's "generation
    is not rerun between repetitions" is exactly the case where that refusal
    matters most.

    **No transaction is open across the model call.** ADR-0003 forbids it, and a
    third-party call is the longest wait in this service. The suite is read
    before, the model is asked after, and nothing is written at all.

    **Every failure is an explicit non-pass.** An unconfigured module, a missing
    credential, an unreachable model, and an unusable answer each get their own
    refusal; none of them degrades into an empty-but-successful set, because an
    empty set is a real answer — "the model proposed nothing" — that a reviewer
    would read as a measurement.
    """
    from integrations.google_evals.generation import LiveModelUnavailable, ProposalRejected
    from integrations.google_evals.live import CredentialMaterialRejected, LiveRunUnavailable

    live = settings.live_evaluator
    if live is None:
        state = settings.module("live_evaluator")
        raise ApiError(
            ApiErrorCode.TARGET_UNAVAILABLE,
            "No live model backend is configured in this deployment, so nothing "
            f"here can draft variants — {state.reason} Write the set by hand and "
            "freeze it — the approval FR-100 requires is a person's either way.",
        )

    async with database.reading() as work:
        suite = await BenchmarkService(work, workspace_id).get(benchmark_id)
    if str(suite["status"]) != "draft":
        raise ApiError(
            ApiErrorCode.BENCHMARK_BINDINGS_SEALED,
            f"this suite is {suite['status']}; FR-100 freezes variants before trials "
            "begin, so a new set belongs to a new suite.",
        )
    if json.loads(str(suite["manifest_json"])).get("frozen_variants") is not None:
        raise ApiError(
            ApiErrorCode.PRECONDITION_FAILED,
            "this benchmark already has frozen variants; FR-100 forbids rerunning "
            "generation between repetitions, so a different set is a different "
            "benchmark and needs a new suite.",
        )

    try:
        async with _live_proposer(http_request, live) as proposer:
            candidates = await VariantGenerationService(
                proposer, credential_var=live.credential_var
            ).propose(request.canonical_intent, count=request.count)
    except (LiveRunUnavailable, CredentialUnavailable, LiveModelUnavailable) as unavailable:
        # One code for three causes, because a client's remedy is the same:
        # nothing was drafted, and the manual path is still open. The message
        # separates them; none of the three carries a response body or a
        # credential, by construction in the client.
        raise ApiError(ApiErrorCode.TARGET_UNAVAILABLE, str(unavailable)) from unavailable
    except CredentialMaterialRejected as carried:
        raise ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(carried)) from carried
    except ProposalRejected as rejected:
        raise ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(rejected)) from rejected
    except CoreError as refused:
        # The core's own refusals: a set carrying a confirmation-bypass
        # instruction, or one quoting the credential variable. Statements about
        # what the model wrote, which is why they read as validation failures
        # rather than as availability.
        raise ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(refused)) from refused
    except ValidationError as invalid:
        # The core's *set-level* rules — a duplicate, a bidirectional override,
        # a variant too short to be a paraphrase, one repeating the canonical
        # intent — are raised inside Pydantic validators, so they arrive wrapped.
        # Caught separately rather than widened above, because only `msg` may be
        # forwarded: Pydantic's `input` echoes the rejected text, and this is
        # exactly the material that may carry a secret.
        raise ApiError(
            ApiErrorCode.CONTRACT_VALIDATION_FAILED,
            "The model's proposal was not a set a reviewer could be shown.",
            details=[
                {
                    "path": ".".join(str(part) for part in error["loc"]),
                    "message": error["msg"],
                }
                for error in invalid.errors()
            ],
        ) from invalid

    return {
        "benchmark_id": benchmark_id,
        "canonical_intent": candidates.canonical_intent,
        "variants": [variant.canonical_document() for variant in candidates.variants],
        # Reproducibility metadata FR-093 records for a suite, echoed so the
        # person reviewing can see which model wrote what they are reading. The
        # credential *variable* is not among these: it tells an operator nothing
        # they need while drafting, and the fewer places it is printed the
        # better.
        "model_provider": live.provider,
        "model_name": live.model,
        # Said in the response, not only in the UI: a client that treated this
        # as an approval would be recording a decision nobody made.
        "approved": False,
    }


@router.post("/benchmarks/{benchmark_id}/frozen-variants", status_code=201)
async def freeze_benchmark_variants(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    settings: SettingsDependency,
    request: Annotated[FreezeVariantsRequest, Body()],
) -> dict[str, Any]:
    """FR-100: "approved variants are frozen into the content-hashed benchmark
    manifest before trials begin; generation is not rerun between repetitions".

    The service has enforced both halves since 010 and nothing could reach it,
    so the capability existed and no person could exercise it. This is the door.

    **The whole FR-100 sequence runs here, in its order.** Validate the shape,
    screen for secrets and confirmation-bypass language, record the approval,
    then freeze. Screening precedes the approval because a reviewer must never
    be shown a variant asking them to approve bypassing a safeguard — and
    because a caller cannot skip a step that the next one's argument type
    requires.

    **Nothing awaits inside the transaction.** Validation, screening and
    approval are pure CPU over at most six short strings, so ADR-0003's rule —
    no DB transaction across I/O — holds while the timestamp still comes from
    the one injected clock, which is what keeps a replayed freeze deterministic.

    **The refusals.** An unknown suite is `RESOURCE_NOT_FOUND` and a suite in
    another workspace is indistinguishable from it; a suite past `draft` is
    `BENCHMARK_BINDINGS_SEALED`; a second freeze is `PRECONDITION_FAILED`; and
    anything the core turns down — an oversized set, a held-back variant, an
    approval naming a variant that does not exist — is
    `CONTRACT_VALIDATION_FAILED`, because it is the caller's document to fix.
    """
    live = settings.live_evaluator
    # Deployment knowledge the target-neutral core cannot have: the *name* of
    # the variable a credential lives in. Its value never leaves the server, and
    # a variant quoting the name is a secret being carried in.
    markers = () if live is None else (live.credential_var,)

    async with locks.hold(workspace_id), database.transaction() as work:
        service = BenchmarkService(work, workspace_id)
        try:
            candidates = require_screened(
                validate_candidates(
                    request.canonical_intent,
                    [variant.model_dump(mode="json") for variant in request.variants],
                ),
                extra_secret_markers=markers,
            )
            approved = approve(
                candidates,
                approved_indices=request.approved_indices,
                reviewer=request.reviewer,
                approved_at=work.instant(),
                note=request.note,
            )
        except CoreError as refused:
            raise ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(refused)) from refused
        except ValidationError as invalid:
            # Only `msg` is forwarded, never Pydantic's `input`. The rejected
            # value here is variant text, and a variant is refused in precisely
            # the cases where it may carry a secret — echoing it would put the
            # value in a response and, from there, in whatever logs that
            # response. The same rule the malformed-request handler follows.
            raise ApiError(
                ApiErrorCode.CONTRACT_VALIDATION_FAILED,
                "That variant set was not accepted.",
                details=[
                    {
                        "path": ".".join(str(part) for part in error["loc"]),
                        "message": error["msg"],
                    }
                    for error in invalid.errors()
                ],
            ) from invalid

        frozen_content_hash = await service.freeze_variants(benchmark_id, approved)
        # Re-read inside the same transaction: the manifest hash moved when the
        # variants landed, and FR-100 makes that new value the identity of the
        # thing a repetition is measured under. A caller that had to fetch it
        # separately could quote a hash from before its own freeze.
        suite = await service.get(benchmark_id)

    return {
        "benchmark_id": benchmark_id,
        "frozen_variants_content_hash": frozen_content_hash,
        "manifest_content_hash": str(suite["manifest_content_hash"]),
        "variant_count": len(approved.approved),
        "reviewer": approved.approval.reviewer,
    }


@router.put("/benchmarks/{benchmark_id}/bindings")
async def save_bindings(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    request: Annotated[BindingsRequest, Body()],
) -> dict[str, Any]:
    """§15.6: "validate and save explicit one-to-one trial bindings before the
    suite becomes ready".

    Every binding in one transaction: FR-091's guarantee is about the set, and a
    partially applied batch would leave a suite whose bindings nobody chose.
    """
    async with locks.hold(workspace_id), database.transaction() as work:
        service = BenchmarkService(work, workspace_id)
        suite = await service.get(benchmark_id)
        mode = CorrelationMode(str(suite["correlation_mode"]))
        for binding in request.bindings:
            try:
                model = TrialBinding(
                    external_trial_id=binding.external_trial_id,
                    correlation_mode=mode,
                    outcome_run_id=binding.outcome_run_id,
                    evaluation_run_id=binding.evaluation_run_id,
                )
            except CoreError as invalid:
                # A binding naming both references or neither is ambiguity in
                # the request, not a malformed document.
                raise ApiError(ApiErrorCode.TRIAL_BINDING_AMBIGUOUS, str(invalid)) from invalid
            await service.bind(
                benchmark_id,
                model,
                acknowledge_unaddressable=binding.acknowledge_unaddressable,
            )
        status = await service.seal(benchmark_id) if request.seal else None

    return {
        "benchmark_id": benchmark_id,
        "bound": len(request.bindings),
        "status": status.value if status is not None else str(suite["status"]),
    }


@router.post("/benchmarks/{benchmark_id}/replay")
async def replay_benchmark(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    registry: RegistryDependency,
) -> dict[str, Any]:
    """§15.6: "execute eligible `imported_trajectory_replay` trials in isolated
    eval workspaces".

    Deliberately outside the workspace lock. Each replay creates its own eval
    workspace and its own transactions, and holding the caller's write lock
    across that I/O would violate ADR-0003's rule that nothing async holds a
    lock across a wait.
    """
    from actionwitness_service.application.benchmark_replay import (
        BenchmarkReplayService,
        TrialReplayInput,
        stored_trajectory,
    )
    from actionwitness_service.application.workspaces import WorkspaceStore

    async with database.reading() as work:
        service = BenchmarkService(work, workspace_id)
        suite = await service.get(benchmark_id)
        rows = await service.trials(benchmark_id)

    if CorrelationMode(str(suite["correlation_mode"])) is not (
        CorrelationMode.IMPORTED_TRAJECTORY_REPLAY
    ):
        raise ApiError(
            ApiErrorCode.PRECONDITION_FAILED,
            "Only an imported_trajectory_replay suite replays; an executed_browser "
            "suite binds to runs that already happened (FR-091).",
        )

    async with database.transaction() as work:
        await BenchmarkService(work, workspace_id).start(benchmark_id)

    contract, adapter_id = await _scenario_inputs(database, registry, workspace_id)
    replayer = BenchmarkReplayService(database, registry, WorkspaceStore(database))
    replayed = []
    for row in rows:
        trajectory = stored_trajectory(row["metadata_json"])
        outcome = await replayer.replay(
            TrialReplayInput(
                trial_row_id=str(row["id"]),
                external_trial_id=str(row["external_trial_id"]),
                trajectory=trajectory,
                contract=contract,
                scenario=_scenario_of(row),
            ),
            owner_workspace_id=workspace_id,
            adapter_id=adapter_id,
        )
        replayed.append(
            {
                "external_trial_id": outcome.external_trial_id,
                "outcome_result": outcome.outcome_result.value,
                "eligibility": outcome.eligibility.value,
                "exclusion_reason": (
                    None if outcome.exclusion_reason is None else outcome.exclusion_reason.value
                ),
                "evaluation_run_id": outcome.evaluation_run_id,
            }
        )

    return {"benchmark_id": benchmark_id, "replayed": replayed}


@router.post("/benchmarks/{benchmark_id}/finalize")
async def finalize_benchmark(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    locks: LocksDependency,
    artifacts: ArtifactsDependency,
) -> dict[str, Any]:
    """§15.6: "validate coverage and create an immutable derived benchmark
    artifact".

    §16.4's error path is honoured here rather than left to the caller: a
    refusal rolls its own transaction back, and the suite is then marked `error`
    in a fresh one so no partial result survives.
    """
    try:
        # Three phases, and the middle one is why: the report is written with no
        # transaction open (ADR-0003), because a file write inside
        # `BEGIN IMMEDIATE` holds SQLite's single writer against every other
        # workspace. The workspace lock spans all three, so the suite cannot
        # move underneath the write.
        async with locks.hold(workspace_id):
            async with database.reading() as work:
                prepared = await BenchmarkService(work, workspace_id).prepare_finalize(benchmark_id)
            written = write_benchmark_report(artifacts, workspace_id, prepared)
            async with database.transaction() as work:
                artifact_id = await BenchmarkService(work, workspace_id).seal_finalize(
                    benchmark_id, prepared, written, artifacts
                )
    except CoreError as refused:
        async with locks.hold(workspace_id), database.transaction() as work:
            await BenchmarkService(work, workspace_id).mark_error(benchmark_id)
        raise ApiError(ApiErrorCode.PRECONDITION_FAILED, str(refused)) from refused

    async with database.reading() as work:
        suite = await BenchmarkService(work, workspace_id).get(benchmark_id)
    return {
        "benchmark_id": benchmark_id,
        "status": str(suite["status"]),
        "result_artifact_id": artifact_id,
    }


@router.get("/benchmarks")
async def list_benchmarks(
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """The suites this workspace owns, so a person can pick one.

    Added because the matrix had no door: every other benchmark route needs an
    id the caller already holds, which is workable for an API client and
    useless to somebody looking at a screen. The listing is deliberately thin —
    enough to identify and choose a suite, and nothing that would tempt a client
    to render a matrix from it instead of reading the suite.
    """
    async with database.reading() as work:
        suites = await BenchmarkService(work, workspace_id).list_suites()

    return {
        "benchmarks": [
            {
                "benchmark_id": str(suite["id"]),
                "status": str(suite["status"]),
                "source_kind": str(suite["source_kind"]),
                "correlation_mode": str(suite["correlation_mode"]),
                "result_artifact_id": suite["result_artifact_id"],
                "created_at": str(suite["created_at"]),
            }
            for suite in suites
        ]
    }


@router.get("/benchmarks/{benchmark_id}")
async def read_benchmark(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.6: "status, metadata, matrix, metrics, and trial summaries"."""
    async with database.reading() as work:
        service = BenchmarkService(work, workspace_id)
        suite = await service.get(benchmark_id)
        summary = await service.summarize(benchmark_id)

    return {
        "benchmark_id": benchmark_id,
        "status": str(suite["status"]),
        # AC-16: the source kind is shown and never represented as a live run.
        "source_kind": str(suite["source_kind"]),
        "correlation_mode": str(suite["correlation_mode"]),
        "normalized_adapter_version": str(suite["normalized_adapter_version"]),
        "result_artifact_id": suite["result_artifact_id"],
        "manifest": json.loads(str(suite["manifest_json"])),
        # FR-100 seals the approved variants into a *content-hashed* manifest,
        # so the hash is what a reader quotes when they say which manifest a
        # repetition ran under. Recomputing it from the document above would be
        # a second opinion on an identity the row already holds.
        "manifest_content_hash": str(suite["manifest_content_hash"]),
        "counts": summary.counts.canonical_document(),
        "metrics": summary.metrics.canonical_document(),
        "by_scenario": [group.canonical_document() for group in summary.by_scenario],
        "by_failure_profile": [group.canonical_document() for group in summary.by_failure_profile],
        "trials": [
            {
                "external_trial_id": trial.external_trial_id,
                "scenario_id": trial.scenario_id,
                "call_level_result": trial.call_level_result.value,
                "outcome_result": trial.outcome_result.value,
                "eligibility": trial.eligibility.value,
                "exclusion_reason": (
                    None if trial.exclusion_reason is None else trial.exclusion_reason.value
                ),
                "addressable": trial.addressable,
            }
            for trial in summary.trials
        ],
    }


@router.get("/benchmarks/{benchmark_id}/trials/{trial_id}")
async def read_trial(
    benchmark_id: BenchmarkId,
    trial_id: TrialId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
) -> dict[str, Any]:
    """§15.6: "bounded redacted call-level and outcome evidence for one trial".

    Bounded: the trajectory is a list of tool names and their already-redacted
    arguments, and the response carries no evaluator prose. §20.3 keeps the full
    document in the immutable source artifact, where an auditor reads it
    deliberately rather than through a list view.
    """
    from actionwitness_service.application.benchmark_metrics import trial_from_row

    async with database.reading() as work:
        rows = await BenchmarkService(work, workspace_id).trials(benchmark_id)
    match = next((row for row in rows if str(row["external_trial_id"]) == trial_id), None)
    if match is None:
        raise ApiError(ApiErrorCode.RESOURCE_NOT_FOUND, f"no trial {trial_id!r} here")

    trial = trial_from_row(match)
    return {
        "external_trial_id": trial.external_trial_id,
        "scenario_id": trial.scenario_id,
        "correlation_mode": trial.correlation_mode.value,
        "call_level_result": trial.call_level_result.value,
        "outcome_result": trial.outcome_result.value,
        "eligibility": trial.eligibility.value,
        "exclusion_reason": (
            None if trial.exclusion_reason is None else trial.exclusion_reason.value
        ),
        "addressable": trial.addressable,
        "outcome_run_id": trial.outcome_run_id,
        "evaluation_run_id": trial.evaluation_run_id,
        "scenario_mode": trial.scenario_mode,
        "failure_profile": trial.failure_profile,
        "trajectory": [dict(step) for step in trial.trajectory],
        "unsupported_metadata": dict(trial.metadata),
        "source_artifact_id": str(match["external_source_artifact_id"]),
    }


@router.get("/benchmarks/{benchmark_id}/report")
async def download_benchmark_report(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    artifacts: ArtifactsDependency,
) -> Response:
    """§15.6: "download the immutable benchmark JSON report".

    The stored bytes, verbatim. A reader must be able to recompute the content
    hash and get the same answer, which only holds if they receive what was
    written rather than a re-serialisation of it.
    """
    async with database.reading() as work:
        suite = await BenchmarkService(work, workspace_id).get(benchmark_id)
        artifact_id = suite["result_artifact_id"]
        if artifact_id is None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "This benchmark has not been finalized, so there is no report yet.",
            )
        relative_path = await artifacts.relative_path(work, workspace_id, str(artifact_id))
    if relative_path is None:  # pragma: no cover - finalization commits both together
        raise ApiError(ApiErrorCode.HARNESS_ERROR, "The benchmark artifact has gone.")

    return Response(
        content=artifacts.read_bytes(relative_path),
        media_type="application/json",
    )


class RepeatedTrialsRequest(_Body):
    """§26.5: run one frozen variant again, N times.

    **`trials` carries no upper bound here.** The ceiling lives in
    `BenchmarkService.MAX_TRIAL_REPETITIONS`, which is where the repetitions are
    actually written, so there is exactly one number to change and exactly one
    place that refuses. A `le=` here would be a second copy free to drift, and a
    request that got past it would still have to be refused underneath.

    **`variant_index` is named, never inferred.** FR-100 froze the set in a
    definite order, and which variant a batch measures is the caller's statement
    about what the resulting rate is a rate *of*. A suite with no frozen set
    leaves it `None`, and the correlation view then groups by scenario — which
    normalization already treats as the shared intent repeated trials repeat.
    """

    source_external_trial_id: Annotated[str, Field(min_length=1, max_length=128)]
    trials: Annotated[int, Field(ge=1)] = 1
    variant_index: Annotated[int, Field(ge=0)] | None = None


@router.post("/benchmarks/{benchmark_id}/repeated-trials", status_code=201)
async def run_repeated_trials(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    registry: RegistryDependency,
    request: Annotated[RepeatedTrialsRequest, Body()],
) -> dict[str, Any]:
    """§26.5: "six intent variants with five repeated trials each".

    One sample cannot characterise a non-deterministic agent — it can only say
    what happened once. Running the *same* frozen variant N times is what turns
    "the observed state disagreed with the evaluator here" into a rate, and
    `/correlation` is where that rate is read.

    **Deliberately outside the workspace lock**, for the same reason `/replay`
    is: every repetition creates its own eval workspace and its own
    transactions, and holding the caller's write lock across that I/O would
    break ADR-0003's rule that nothing async holds a lock across a wait. The
    guard that matters is not the lock but the state check `record_repetition`
    repeats before each insert — a suite sealed midway stops the batch instead of
    growing a population that was already closed.

    **A partial batch is a real answer.** Each repetition's row is committed
    before it runs, so a cancelled request leaves the repetitions it started
    visible as excluded rather than absent, and nothing retries them. `201` says
    trials were created; the body says what each of them concluded, including
    the ones that concluded nothing.
    """
    from actionwitness_service.application.benchmark_replay import RepeatedTrialService
    from actionwitness_service.application.workspaces import WorkspaceStore

    async with database.reading() as work:
        plan = await BenchmarkService(work, workspace_id).plan_repetitions(
            benchmark_id,
            source_external_trial_id=request.source_external_trial_id,
            count=request.trials,
            variant_index=request.variant_index,
        )

    contract, adapter_id = await _scenario_inputs(database, registry, workspace_id)
    completed = await RepeatedTrialService(database, registry, WorkspaceStore(database)).run(
        plan,
        workspace_id=workspace_id,
        contract=contract,
        adapter_id=adapter_id,
    )
    return {
        "benchmark_id": benchmark_id,
        "source_external_trial_id": plan.source_external_trial_id,
        "variant_index": plan.variant_index,
        "trials": len(completed),
        "repetitions": [
            {
                "external_trial_id": trial.external_trial_id,
                "repetition_index": trial.repetition_index,
                # The two layers stay two fields. `call_level_result` is
                # deliberately absent from this response: a repetition adds no
                # new evaluator evidence, and repeating the imported verdict
                # beside a fresh outcome would invite a reader to count it as a
                # second measurement.
                "outcome_result": trial.outcome_result.value,
                "eligibility": trial.eligibility.value,
                "exclusion_reason": (
                    None if trial.exclusion_reason is None else trial.exclusion_reason.value
                ),
                "evaluation_run_id": trial.evaluation_run_id,
            }
            for trial in completed
        ],
    }


@router.get("/benchmarks/{benchmark_id}/correlation")
async def read_benchmark_correlation(
    benchmark_id: BenchmarkId,
    workspace_id: WorkspaceDependency,
    database: DatabaseDependency,
    settings: SettingsDependency,
) -> dict[str, Any]:
    """The evaluator's verdicts against the deterministic ones, per variant.

    `GET /benchmarks/{id}` already reports the suite-wide two-by-two. This
    reports it *per population*, with the outcome distribution beside the
    evaluator's own, because that is the shape repetition produces: one variant
    run five times can pass three and fail two, and a suite-wide cell cannot say
    so.

    Read-only and computed on every request rather than stored. Until
    finalization these numbers are a view, and a cached copy would go stale the
    moment another repetition landed — the same reason `summarize` recomputes.

    An empty `populations` means no trials have run, not that the trials that ran
    found nothing. The two are different findings and the caller is left able to
    tell them apart.
    """
    from actionwitness_service.application.benchmark_correlation import (
        correlate,
        variant_trials,
    )

    async with database.reading() as work:
        service = BenchmarkService(work, workspace_id)
        suite = await service.get(benchmark_id)
        rows = await service.trials(benchmark_id)

    manifest = json.loads(str(suite["manifest_json"]))
    frozen = manifest.get("frozen_variants") if isinstance(manifest, dict) else None
    populations = correlate(
        variant_trials(rows, frozen_variants=frozen if isinstance(frozen, dict) else None)
    )
    return {
        "benchmark_id": benchmark_id,
        "status": str(suite["status"]),
        # AC-16 again: a correlation view is still a claim about where these
        # trials came from, and it must never read as a live execution.
        "source_kind": str(suite["source_kind"]),
        "correlation_mode": str(suite["correlation_mode"]),
        "repetition_ceiling": MAX_TRIAL_REPETITIONS,
        # The left axis of this matrix is an *imported* evaluator verdict, so a
        # deployment with evaluator import switched off can never populate it.
        # Reported here rather than left for a client to infer, because an empty
        # axis with no explanation reads as "the evaluator found nothing" — a
        # measurement claim — instead of "no evaluator result can reach this
        # deployment at all", which is a configuration fact.
        "evaluator_import_available": settings.evaluator_import is not None,
        "populations": [population.canonical_document() for population in populations],
    }


def _rejection(rejected: Exception) -> ApiError:
    """An unreadable report is about the *file*, never about the target.

    422 rather than 409: the document cannot be made acceptable by retrying, and
    a caller needs to know it is theirs to fix.
    """
    return ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(rejected))


def _scenario_of(row: Any) -> Any:
    """The scenario a replayed trial runs under.

    Taken from the trial's own recorded columns. A replay that read the
    workspace's *current* scenario would judge the trial against a
    configuration nobody recorded for it.
    """
    from actionwitness_core.ports.models import ScenarioSelection

    return ScenarioSelection(
        scenario_mode=str(row["scenario_mode"] or "post_fix"),
        fault_profile=row["failure_profile"],
    )


async def _scenario_inputs(database: Any, registry: Any, workspace_id: str) -> tuple[Any, str]:
    """The contract that judges a replay, and the adapter that runs it.

    §24.7 step 1 puts the contract in the *scenario*. Until a manifest carries
    one per scenario, the workspace's selected contract is what a replay is
    judged against — and a suite with no contract selected is refused rather
    than judged against nothing.
    """
    from actionwitness_core.contracts.models import OutcomeContract

    from actionwitness_service.application.contract_service import ContractService
    from actionwitness_service.application.workspaces import WorkspaceStore

    async with database.reading() as work:
        row = await WorkspaceStore(database).get(work, workspace_id)
        contract_id = None if row is None else row["selected_contract_id"]
        if contract_id is None:
            raise ApiError(
                ApiErrorCode.PRECONDITION_FAILED,
                "Select an outcome contract before replaying: a replay with no "
                "contract has nothing to judge the target against.",
            )
        document = await ContractService(work, workspace_id, registry).stored_document(
            str(contract_id)
        )
    if document is None:  # pragma: no cover - contracts are immutable
        raise ApiError(ApiErrorCode.HARNESS_ERROR, "The selected contract has gone.")
    # A workspace names a *target* (`buggy-store`); the registry is keyed by
    # *module* (`buggy_store`). `resolve` accepts either, and resolving here
    # means the replay reaches the same adapter the run path would.
    slot = registry.resolve(row["selected_target_id"] if row else None)
    if slot is None:
        raise ApiError(
            ApiErrorCode.TARGET_UNAVAILABLE,
            "No target adapter is selected for this workspace, so there is nothing "
            "to replay the imported trajectory through.",
        )
    contract = OutcomeContract.model_validate(json.loads(document))
    return contract, slot.name
