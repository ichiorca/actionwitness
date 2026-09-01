"""Building a case from a run's immutable evidence (§24.2, FR-080, FR-081).

Pure, and deliberately so. The factory takes evidence a caller already loaded
and returns a document; it opens no database, reads no clock, and mints no
identifier. That is what makes FR-080's idempotence a property of the *content*
rather than of a uniqueness check: run it twice on the same evidence and the
bytes are identical, because nothing inside varies.

The ordering rules §24.2 sets out are load-bearing, not stylistic:

- **The contract's stored hash is verified before anything else** (step 6). A
  case built around a contract that had already drifted would reproduce
  nothing, and would do it convincingly.
- **The hash is calculated last** (step 11). Every other field is final first,
  so the hash describes the document that carries it.
- **`current` expects a clean pass and `reproduce_source` expects the source's
  exact outcome** (steps 7–8). Neither is inferred from the other.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from actionwitness_core.contracts.models import OutcomeContract
from actionwitness_core.engine.enums import FailureClassification
from actionwitness_core.evals.enums import ConfirmationStrategy, EvalEnvironment, SourceProtocol
from actionwitness_core.evals.models import (
    EmbeddedContract,
    EnvironmentExpectation,
    EvalExpectations,
    EvalFixture,
    EvalSource,
    EvalTarget,
    RecordedDecision,
    RegressionEvalCase,
    ReplayConfiguration,
    SourceFinding,
    SurfaceEvidence,
    TrajectoryStep,
)
from actionwitness_core.kernel import ContractError, CoreErrorCode, JsonValue
from actionwitness_core.reports.enums import LayerResult
from actionwitness_core.security.canonical import content_hash

__all__ = ["ELIGIBLE_SOURCE_RESULTS", "build_case", "case_identifier"]

#: FR-080: "only failed or warning-bearing terminal outcome runs may generate
#: regression eval cases." A passing run has no failure to reproduce, so a case
#: cut from one would assert that nothing goes wrong — which every future build
#: would satisfy right up until it didn't.
ELIGIBLE_SOURCE_RESULTS: frozenset[LayerResult] = frozenset(
    {LayerResult.FAILED, LayerResult.PASSED_WITH_WARNINGS}
)


def case_identifier(source_run_id: str, contract_content_hash: str, generator_version: str) -> str:
    """FR-080's idempotence key, rendered as the case's own id.

    Derived rather than random: two generations from the same evidence must
    produce the same document, and a fresh `uuid4()` in the id would break that
    in the one field a reader trusts most.
    """
    digest = content_hash(
        {
            "source_run_id": source_run_id,
            "contract_content_hash": contract_content_hash,
            "generator_schema_version": generator_version,
        }
    )
    return f"eval_{digest.removeprefix('sha256:')[:32]}"


def build_case(
    *,
    name: str,
    source_run_id: str,
    implementation_version: str,
    build_commit: str | None,
    scenario_mode: str | None,
    failure_profile: str | None,
    source_result: LayerResult,
    source_classifications: Sequence[FailureClassification],
    target_type: str,
    target_id: str,
    adapter_id: str,
    contract: OutcomeContract,
    stored_contract_hash: str,
    fixture_state: Mapping[str, JsonValue],
    fixture_state_version: int,
    fixture_is_complete: bool,
    trajectory: Sequence[tuple[int, str, Mapping[str, JsonValue]]],
    source_findings: Sequence[SourceFinding] = (),
    surface: SurfaceEvidence | None = None,
    non_replayable_policies: Sequence[str] = (),
    confirmation_strategy: ConfirmationStrategy = ConfirmationStrategy.NO_CONFIRMATION,
    recorded_decisions: Sequence[RecordedDecision] = (),
    generator_version: str,
) -> RegressionEvalCase:
    """Assemble one case. Same evidence in, byte-identical case out."""
    if source_result not in ELIGIBLE_SOURCE_RESULTS:
        raise ContractError(
            f"only failed or warning-bearing runs generate eval cases; this run "
            f"is {source_result.value}",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )

    # §24.2 step 6, first: verify the contract we were handed is the contract
    # the run was judged against, before a single field is written.
    recomputed = content_hash(contract.canonical_document())
    if recomputed != stored_contract_hash:
        raise ContractError(
            "the source contract does not match its stored hash; the case would "
            "reproduce a contract the run never used",
            code=CoreErrorCode.EVALUATION_INPUT_INVALID,
        )

    steps = tuple(
        TrajectoryStep(sequence=sequence, tool=tool, arguments=dict(arguments))
        for sequence, tool, arguments in trajectory
    )

    critical = tuple(source_classifications)
    return RegressionEvalCase(
        id=case_identifier(source_run_id, stored_contract_hash, generator_version),
        name=name,
        source=EvalSource(
            protocol=SourceProtocol.WEBMCP,
            run_id=source_run_id,
            implementation_version=implementation_version,
            build_commit=build_commit,
            scenario_mode=scenario_mode,
            failure_profile=failure_profile,
            overall_result=source_result,
            critical_classifications=critical,
        ),
        target=EvalTarget(type=target_type, id=target_id, adapter=adapter_id),
        fixture=EvalFixture(
            state_version=fixture_state_version,
            content_hash=content_hash(dict(fixture_state)),
            target_state=dict(fixture_state),
            complete=fixture_is_complete,
        ),
        trajectory=steps,
        contract=EmbeddedContract(content_hash=stored_contract_hash, document=contract),
        source_findings=tuple(source_findings),
        surface=surface,
        non_replayable_policies=tuple(non_replayable_policies),
        replay=ReplayConfiguration(
            # §24.4: "generated eval cases never silently force
            # `reproduce_source`. `current` is always the default."
            default_environment=EvalEnvironment.CURRENT,
            confirmation_strategy=confirmation_strategy,
            recorded_decisions=tuple(recorded_decisions),
        ),
        expected=EvalExpectations(
            # Step 7: the corrected implementation is expected to be clean.
            current=EnvironmentExpectation(
                overall_result=LayerResult.PASSED, required_classifications=()
            ),
            # Step 8: the source's outcome and its exact classification set —
            # copied from the recording, never inferred from the contract.
            reproduce_source=EnvironmentExpectation(
                overall_result=source_result, required_classifications=critical
            ),
        ),
    )
