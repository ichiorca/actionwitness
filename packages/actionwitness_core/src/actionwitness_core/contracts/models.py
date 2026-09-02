"""Outcome-contract models: the §10 format as validated, immutable records.

Spec v1.9 §10.1 (canonical document), §10.2 (Pydantic model requirements),
§10.3 (expected-tool semantics), §10.4 (limits), §17.1 (`contracts` is
insert-only), §17.2 (the contract hash "covers its validated contract document").

A contract is untrusted input that decides what "correct" means, so every rule in
§10.2 is enforced here rather than at a call site. Two of them are worth naming:

* **An operator that takes no expected value must not be given one.** Ignoring a
  stray `value` on an `exists` assertion would let an author believe they had
  asserted a value when they had asserted only presence.
* **The canonical document, not the submitted bytes, is what is bounded and
  hashed.** §17.2 hashes "its validated contract document", so whitespace and
  member order in the submission change neither the size check nor the identity.

`validate_against_target` takes plain names rather than an adapter object: the
adapter registry lives in the application layer, and a contract model that
imported a registry would drag composition into the domain.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Literal

from pydantic import (
    Field,
    PlainSerializer,
    PlainValidator,
    StringConstraints,
    ValidationError,
    WithJsonSchema,
    model_validator,
)

from actionwitness_core.contracts.enums import (
    OPERATORS_REQUIRING_VALUE,
    TRANSITION_OPERATORS,
    AssertionOperator,
    AssertionSeverity,
    PolicyType,
    SurfaceDeltaKind,
)
from actionwitness_core.contracts.limits import (
    MAX_ASSERTIONS,
    MAX_CANONICAL_CONTRACT_BYTES,
    MAX_DESCRIPTION_LENGTH,
    MAX_EXPECTED_TOOLS,
    MAX_INTENT_LENGTH,
    MAX_NAME_LENGTH,
    MAX_POLICIES,
    MAX_REDACTION_PATHS,
)
from actionwitness_core.contracts.paths import ObservationPathField
from actionwitness_core.kernel import (
    ContractError,
    CoreErrorCode,
    CoreModel,
    ErrorDetail,
    JsonValue,
    UtcInstant,
)
from actionwitness_core.security.canonical import canonicalize, content_hash
from actionwitness_core.security.limits import MAX_TOOL_NAME_CHARS
from actionwitness_core.security.redaction import RedactionPattern, RedactionPolicy

__all__ = [
    "SUPPORTED_CONTRACT_SCHEMA_VERSIONS",
    "Assertion",
    "ContractRecord",
    "ExpectedTools",
    "ForbiddenToolPolicy",
    "IdempotencyPolicy",
    "MaximumMutationsPolicy",
    "NoUndeclaredChangesPolicy",
    "OutcomeContract",
    "Policy",
    "Precondition",
    "RedactionSpec",
    "RequiresConfirmationPolicy",
    "StableToolSurfacePolicy",
    "contract_json_schema",
]

#: Contract document versions this build understands. §10.2: "reject unknown
#: schema versions" - an unrecognised version is refused, never best-effort
#: parsed, because a field this build ignores is a check the author believes is
#: running.
SUPPORTED_CONTRACT_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0"})


def _fail(message: str, *details: ErrorDetail) -> ContractError:
    return ContractError(
        message,
        code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
        details=details or (ErrorDetail(location="$", message=message),),
    )


def _parse_pattern(value: object) -> RedactionPattern:
    return value if isinstance(value, RedactionPattern) else RedactionPattern.parse(value)


#: A restricted dotted observation path (§10.2), shared with the port models so
#: one grammar is validated in exactly one place.
type ContractPath = ObservationPathField

#: A redaction glob (§20.3), which uses a *different* grammar from a path.
type RedactionGlob = Annotated[
    RedactionPattern,
    PlainValidator(_parse_pattern),
    PlainSerializer(str, return_type=str),
    WithJsonSchema({"type": "string", "description": "Redaction glob pattern (§20.3)"}),
]

#: A target-tool name, bounded by the §11.4 tool-context budget.
type ToolName = Annotated[
    str, StringConstraints(min_length=1, max_length=MAX_TOOL_NAME_CHARS, strip_whitespace=False)
]


class Precondition(CoreModel):
    """One check against the initial snapshot (§9.4).

    Transition operators are refused here because a precondition sees only one
    snapshot; `unchanged` against a single snapshot is trivially true and would
    read as a check that had passed.
    """

    path: ContractPath
    operator: AssertionOperator
    value: JsonValue = None

    @model_validator(mode="after")
    def _check_operator_and_value(self) -> Precondition:
        if self.operator in TRANSITION_OPERATORS:
            raise _fail(
                f"operator {self.operator.value!r} compares two snapshots and is invalid "
                "in a precondition",
                ErrorDetail(
                    location=f"preconditions[{self.path}].operator",
                    message="transition operators are invalid in a precondition",
                ),
            )
        _check_value_presence(self, f"preconditions[{self.path}]")
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return _term_document(self)


class Assertion(CoreModel):
    """One business check against the final snapshot (§9.4, FR-052)."""

    id: Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_LENGTH)]
    path: ContractPath
    operator: AssertionOperator
    value: JsonValue = None
    #: Defaults to `critical`: an assertion whose severity was forgotten should
    #: fail the run rather than quietly become advisory.
    severity: AssertionSeverity = AssertionSeverity.CRITICAL

    @model_validator(mode="after")
    def _check_value(self) -> Assertion:
        _check_value_presence(self, f"assertions[{self.id}]")
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"id": self.id, **_term_document(self), "severity": self.severity.value}


def _check_value_presence(term: Precondition | Assertion, location: str) -> None:
    """Enforce §10.2's "require an expected value only for operators that need one".

    Presence is read from `model_fields_set` rather than from the value, because
    `value: null` is a legitimate expectation for `equals` and must be
    distinguishable from an omitted value.
    """
    supplied = "value" in term.model_fields_set
    needs_value = term.operator in OPERATORS_REQUIRING_VALUE
    if needs_value and not supplied:
        raise _fail(
            f"operator {term.operator.value!r} requires an expected value",
            ErrorDetail(location=f"{location}.value", message="expected value is required"),
        )
    if not needs_value and supplied:
        raise _fail(
            f"operator {term.operator.value!r} takes no expected value; supplying one "
            "would read as an assertion that is not being made",
            ErrorDetail(location=f"{location}.value", message="expected value is not allowed"),
        )


def _term_document(term: Precondition | Assertion) -> dict[str, JsonValue]:
    document: dict[str, JsonValue] = {
        "path": str(term.path),
        "operator": term.operator.value,
    }
    if "value" in term.model_fields_set:
        document["value"] = term.value
    return document


class ExpectedTools(CoreModel):
    """The optional observed-trajectory term (§10.3).

    Omitting the whole block leaves observed trajectory `not_evaluated`; supplying
    it with an empty `calls` list would instead assert nothing while looking like
    a check, so an empty list is refused.
    """

    ordered: bool = False
    calls: Annotated[tuple[ToolName, ...], Field(min_length=1, max_length=MAX_EXPECTED_TOOLS)]

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"ordered": self.ordered, "calls": list(self.calls)}


class RequiresConfirmationPolicy(CoreModel):
    """§9.5 / FR-060: a successful protected mutation needs a prior approval."""

    type: Literal[PolicyType.REQUIRES_CONFIRMATION] = PolicyType.REQUIRES_CONFIRMATION
    tool: ToolName
    #: FR-062 fixes both the range and the default.
    timeout_seconds: Annotated[int, Field(ge=10, le=300)] = 60

    def canonical_document(self) -> dict[str, JsonValue]:
        return {
            "type": self.type.value,
            "tool": self.tool,
            "timeout_seconds": self.timeout_seconds,
        }


class IdempotencyPolicy(CoreModel):
    """§9.5 / FR-063: a repeated request ID may change state at most once."""

    type: Literal[PolicyType.IDEMPOTENT_BY_REQUEST_ID] = PolicyType.IDEMPOTENT_BY_REQUEST_ID
    tool: ToolName

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"type": self.type.value, "tool": self.tool}


class MaximumMutationsPolicy(CoreModel):
    """§9.5 / FR-064: qualifying state-changing completions are capped."""

    type: Literal[PolicyType.MAXIMUM_MUTATIONS] = PolicyType.MAXIMUM_MUTATIONS
    limit: Annotated[int, Field(ge=0)]

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"type": self.type.value, "limit": self.limit}


class ForbiddenToolPolicy(CoreModel):
    """§9.5 / FR-065: any invocation start for this tool fails the policy."""

    type: Literal[PolicyType.FORBIDDEN_TOOL] = PolicyType.FORBIDDEN_TOOL
    tool: ToolName

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"type": self.type.value, "tool": self.tool}


class NoUndeclaredChangesPolicy(CoreModel):
    """§9.5 / §9.10: no undeclared canonical state path may change.

    Every `allow_paths` entry is a waiver, and §23.1 requires each applied waiver
    to appear in the report, so they are modelled as first-class paths rather
    than as free text.
    """

    type: Literal[PolicyType.NO_UNDECLARED_CHANGES] = PolicyType.NO_UNDECLARED_CHANGES
    allow_paths: tuple[ContractPath, ...] = ()

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"type": self.type.value, "allow_paths": [str(path) for path in self.allow_paths]}


class StableToolSurfacePolicy(CoreModel):
    """§9.5: the target tool surface may not change outside a declared delta.

    The default set is §9.5's: `added`, `removed`, `schema_change` and
    `hint_change` fail, while `description_change` is a warning "because benign
    copy edits should not fail a run".
    """

    type: Literal[PolicyType.STABLE_TOOL_SURFACE] = PolicyType.STABLE_TOOL_SURFACE
    # NOTE (014-T4): `declared_churn_tools` — an exact-name allowlist of target
    # tools whose mid-run appearance is expected — is specified by 014's scope
    # and is NOT here. Adding a field to this model changes
    # `regression_eval_case_1_0.json`, which is a *published* artifact inside the
    # protected eval corpus, and republishing it is an operator decision rather
    # than a side effect of a feature branch.
    #
    # §9.11's partition already excuses the case 014's scope actually names —
    # the 006 phase-driven harness tool set — structurally, by namespace, which
    # is stronger than an allowlist. What is missing is the ability to excuse a
    # *target* tool that legitimately churns, which no shipped contract needs.
    # See the 014 deviations ledger.
    failing_delta_kinds: tuple[SurfaceDeltaKind, ...] = (
        SurfaceDeltaKind.ADDED,
        SurfaceDeltaKind.REMOVED,
        SurfaceDeltaKind.SCHEMA_CHANGE,
        SurfaceDeltaKind.HINT_CHANGE,
    )

    @model_validator(mode="after")
    def _check_kinds_are_distinct(self) -> StableToolSurfacePolicy:
        if len(set(self.failing_delta_kinds)) != len(self.failing_delta_kinds):
            raise _fail("failing_delta_kinds contains a duplicate")
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        # Sorted so that two contracts listing the same kinds in different orders
        # are the same contract and hash identically (§17.2).
        return {
            "type": self.type.value,
            "failing_delta_kinds": sorted(kind.value for kind in self.failing_delta_kinds),
        }


#: The closed policy union, discriminated on `type` so an unknown policy type is
#: a rejection rather than a silently dropped term (§10.2).
type Policy = Annotated[
    RequiresConfirmationPolicy
    | IdempotencyPolicy
    | MaximumMutationsPolicy
    | ForbiddenToolPolicy
    | NoUndeclaredChangesPolicy
    | StableToolSurfacePolicy,
    Field(discriminator="type"),
]


class RedactionSpec(CoreModel):
    """The contract's own `redaction` block (§10.1, §20.3).

    Modelled as the nested object §10.1 shows rather than as a flat list, so a
    canonical document round-trips back through the validator unchanged - which
    is what makes the stored document verifiable against its own hash.
    """

    paths: Annotated[tuple[RedactionGlob, ...], Field(max_length=MAX_REDACTION_PATHS)] = ()

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"paths": [str(pattern) for pattern in self.paths]}


class OutcomeContract(CoreModel):
    """One validated, immutable outcome contract (§10)."""

    schema_version: str
    name: Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_LENGTH)]
    target_id: Annotated[str, StringConstraints(min_length=1, max_length=MAX_NAME_LENGTH)]
    intent: Annotated[str, StringConstraints(min_length=1, max_length=MAX_INTENT_LENGTH)]
    description: Annotated[str, StringConstraints(max_length=MAX_DESCRIPTION_LENGTH)] = ""
    preconditions: Annotated[tuple[Precondition, ...], Field(max_length=MAX_ASSERTIONS)] = ()
    expected_tools: ExpectedTools | None = None
    assertions: Annotated[tuple[Assertion, ...], Field(max_length=MAX_ASSERTIONS)] = ()
    policies: Annotated[tuple[Policy, ...], Field(max_length=MAX_POLICIES)] = ()
    redaction: RedactionSpec | None = None

    @model_validator(mode="after")
    def _check_contract(self) -> OutcomeContract:
        if self.schema_version not in SUPPORTED_CONTRACT_SCHEMA_VERSIONS:
            raise ContractError(
                f"unsupported contract schema version {self.schema_version!r}; "
                f"this build understands {sorted(SUPPORTED_CONTRACT_SCHEMA_VERSIONS)}",
                code=CoreErrorCode.UNSUPPORTED_SCHEMA_VERSION,
                details=(
                    ErrorDetail(location="schema_version", message="unsupported schema version"),
                ),
            )
        if not self.assertions and not self.policies:
            raise _fail(
                "a contract must carry at least one assertion or policy; one that carries "
                "neither judges nothing while appearing to"
            )
        identifiers = [assertion.id for assertion in self.assertions]
        duplicates = sorted({name for name in identifiers if identifiers.count(name) > 1})
        if duplicates:
            raise _fail(
                f"duplicate assertion identifiers: {duplicates}",
                *(
                    ErrorDetail(location=f"assertions[{name}].id", message="duplicate identifier")
                    for name in duplicates
                ),
            )
        size = len(canonicalize(self.canonical_document()))
        if size > MAX_CANONICAL_CONTRACT_BYTES:
            raise _fail(
                f"the canonical contract is {size} bytes, over the "
                f"{MAX_CANONICAL_CONTRACT_BYTES}-byte limit"
            )
        return self

    def canonical_document(self) -> dict[str, JsonValue]:
        """The exact document §17.2 hashes and §17.1 stores.

        Built explicitly rather than by `model_dump` so that an optional term is
        absent rather than present-and-null, and so a value-less operator carries
        no `value` member. Round-tripping this document back through the model
        therefore yields the same contract and the same hash.
        """
        document: dict[str, JsonValue] = {
            "schema_version": self.schema_version,
            "name": self.name,
            "target_id": self.target_id,
            "intent": self.intent,
        }
        if self.description:
            document["description"] = self.description
        if self.preconditions:
            document["preconditions"] = [term.canonical_document() for term in self.preconditions]
        if self.expected_tools is not None:
            document["expected_tools"] = self.expected_tools.canonical_document()
        if self.assertions:
            document["assertions"] = [term.canonical_document() for term in self.assertions]
        if self.policies:
            document["policies"] = [policy.canonical_document() for policy in self.policies]
        if self.redaction is not None and self.redaction.paths:
            document["redaction"] = self.redaction.canonical_document()
        return document

    def content_hash(self) -> str:
        """The `sha256:...` identity of this contract's validated document."""
        return content_hash(self.canonical_document())

    def redaction_policy(self) -> RedactionPolicy:
        """The §20.3 policy this contract implies: defaults plus its own patterns."""
        return RedactionPolicy(patterns=self.redaction.paths if self.redaction else ())

    def referenced_tools(self) -> frozenset[str]:
        """Every target-tool name the contract names, from any term."""
        names = set(self.expected_tools.calls) if self.expected_tools else set()
        names |= {
            policy.tool
            for policy in self.policies
            if isinstance(
                policy, RequiresConfirmationPolicy | IdempotencyPolicy | ForbiddenToolPolicy
            )
        }
        return frozenset(names)

    def confirmed_tools(self) -> frozenset[str]:
        """Tools this contract requires a human confirmation for."""
        return frozenset(
            policy.tool
            for policy in self.policies
            if isinstance(policy, RequiresConfirmationPolicy)
        )

    def validate_against_target(
        self,
        *,
        target_id: str,
        tool_names: Iterable[str],
        protected_tools: Iterable[str] = (),
    ) -> None:
        """Apply the §10.2 rules that need the selected adapter's vocabulary.

        Separate from construction because the adapter registry is an application
        concern: a contract is valid on its own terms before a target is chosen,
        and becomes valid *for a target* only once that target's published tool
        specs are known.
        """
        published = frozenset(tool_names)
        protected = frozenset(protected_tools)
        details: list[ErrorDetail] = []

        if target_id != self.target_id:
            details.append(
                ErrorDetail(
                    location="target_id",
                    message=(
                        f"contract targets {self.target_id!r} but the selected adapter is "
                        f"{target_id!r}"
                    ),
                )
            )
        for name in sorted(self.referenced_tools() - published):
            details.append(
                ErrorDetail(
                    location="expected_tools.calls",
                    message=f"{name!r} is not a tool published by the selected adapter",
                )
            )
        # §10.2: "reject destructive policy configurations that omit confirmation
        # requirements". A contract that expects a protected mutation without
        # requiring consent for it would report a missing approval as a policy
        # the author never asked for.
        for name in sorted((self.referenced_tools() & protected) - self.confirmed_tools()):
            details.append(
                ErrorDetail(
                    location="policies",
                    message=(
                        f"{name!r} is a protected mutation, so the contract must carry a "
                        "requires_confirmation policy for it"
                    ),
                )
            )
        if details:
            raise _fail(f"contract is not valid for target {target_id!r}", *details)


class ContractRecord(CoreModel):
    """An immutable stored contract (§17.1: "there is no update operation").

    The document and its hash are stored together so a reader can verify identity
    without re-deriving it from a model version that may have moved on. Identity
    and creation instant are injected rather than generated here, because a core
    that allocated its own identifiers could not be replayed.
    """

    contract_id: Annotated[str, StringConstraints(min_length=1)]
    schema_version: str
    content_hash: Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
    document: Mapping[str, JsonValue]
    created_at: UtcInstant

    @classmethod
    def of(
        cls, contract: OutcomeContract, *, contract_id: str, created_at: object
    ) -> ContractRecord:
        return cls(
            contract_id=contract_id,
            schema_version=contract.schema_version,
            content_hash=contract.content_hash(),
            document=contract.canonical_document(),
            created_at=created_at,  # type: ignore[arg-type]
        )

    def verify(self) -> bool:
        """True when the stored hash still describes the stored document."""
        return content_hash(dict(self.document)) == self.content_hash


def contract_json_schema() -> dict[str, JsonValue]:
    """Export the public contract schema (§10.2, §26.1).

    Derived from the model rather than hand-written, so the published schema and
    the validator cannot disagree - §26.1 requires "exact agreement between
    public schemas and Pydantic models", and two hand-maintained artifacts drift.
    """
    return OutcomeContract.model_json_schema(mode="validation")


def parse_contract(document: Mapping[str, object] | Sequence[object]) -> OutcomeContract:
    """Validate an untrusted contract document into a model.

    Pydantic's `ValidationError` is translated into the project's structured
    error so a WebMCP tool result can name the offending fields (§10.2) without
    the caller having to understand two error shapes.

    A rejection raised by one of this module's own validators is *recovered* from
    inside that `ValidationError` rather than flattened into it. `CoreError`
    derives from `ValueError` so Pydantic collects it with the field that caused
    it, but collecting it must not cost its code: an unsupported schema version
    has to keep reaching the caller as `UNSUPPORTED_SCHEMA_VERSION`, not as a
    generic validation failure.
    """
    if not isinstance(document, Mapping):
        raise _fail(f"a contract must be an object, not {type(document).__name__}")
    try:
        return OutcomeContract.model_validate(dict(document))
    except ContractError:
        raise
    except ValidationError as exc:
        raise _rebuild(exc) from exc


def _rebuild(exc: ValidationError) -> ContractError:
    """Turn a Pydantic failure back into one structured contract error."""
    raised: list[ContractError] = []
    details: list[ErrorDetail] = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "$"
        inner = error.get("ctx", {}).get("error")
        if isinstance(inner, ContractError):
            raised.append(inner)
            details.extend(inner.details or (ErrorDetail(location, inner.message),))
        else:
            details.append(ErrorDetail(location=location, message=error["msg"]))

    codes = {error.code for error in raised}
    if len(raised) == 1:
        return ContractError(raised[0].message, code=raised[0].code, details=details)
    code = codes.pop() if len(codes) == 1 else CoreErrorCode.CONTRACT_VALIDATION_FAILED
    return ContractError("contract validation failed", code=code, details=details)
