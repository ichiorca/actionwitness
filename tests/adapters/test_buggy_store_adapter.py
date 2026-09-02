"""Buggy Store adapter conformance (spec v1.9 §9.1, §13.4, App. D.2; 003-T10/T11).

The exit gate asks these tests to prove "allowlisting, schema validation, effect
metadata, prepare/execute/observe behavior, and no service import".

Two of those carry more weight than the rest.

**No service import** is the milestone's defining boundary and is checked by
module graph rather than by reading the source, so a lazy import inside a
request handler cannot hide from it. BUILD_ORDER invariant 3: "neither path
imports Buggy Store service objects."

**Safe blocks are not failures.** FR-033 makes a denied, expired or cancelled
protected action an expected terminal outcome, so the adapter reports a
*completed* invocation with a blocked status. An adapter that returned a failure
there would make the harness punish the safe behaviour it exists to encourage,
and the consent policy would fail a run that did exactly the right thing.

Everything runs over ADR-0001's `ASGITransport` against the real store app, so
the request path exercised here is the one production uses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from actionwitness_core.evidence.enums import EvidenceSourceClassification, ToolReportedStatus
from actionwitness_core.journeys.enums import EventActor, OutcomeEventType
from actionwitness_core.kernel import ContractError
from actionwitness_core.ports import ManagedTargetAdapter, ObservationProvider
from actionwitness_core.ports.enums import ExecutionMode, RetrySemantics, SideEffectClass
from actionwitness_core.ports.models import ExecutionContext, ScenarioSelection
from buggy_store.api import create_app
from integrations.buggy_store.adapter import MissingHumanConsent
from integrations.buggy_store.observation import PROVENANCE, PROVIDER_ID

from integrations.buggy_store import (
    ADAPTER_ID,
    EFFECT_MAP,
    TARGET_ID,
    TOOL_NAMES,
    TOOL_SPECS,
    BuggyStoreAdapter,
    ToolNotAllowed,
)

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
MUG = "mug-ceramic-001"


@pytest.fixture
async def adapter(tmp_path: Path) -> AsyncIterator[BuggyStoreAdapter]:
    app = create_app(database_path=tmp_path / "store.sqlite3")
    async with (
        app.router.lifespan_context(app),
        httpx.ASGITransport(app=app) as transport,
        httpx.AsyncClient(transport=transport, base_url="http://buggy-store.test") as client,
    ):
        # ADR-0001: the adapter takes the client; it never builds one.
        yield BuggyStoreAdapter(client, clock=lambda: EPOCH)


def _context(sequence: int = 1) -> ExecutionContext:
    return ExecutionContext(
        workspace_id="ws-1",
        run_id="run-1",
        invocation_id=f"inv-{sequence}",
        request_id=f"req-{sequence:>012}",
        correlation_id=f"corr-{sequence}",
        idempotency_key=f"key-{sequence}",
        actor=EventActor.AGENT,
    )


async def _armed(adapter: BuggyStoreAdapter, mode: str = "post_fix", fault: str = "none") -> None:
    await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode=mode, fault_profile=fault))


# --- the published surface (§9.1, FR-015) -----------------------------------


@pytest.mark.adapters
def test_the_adapter_satisfies_the_managed_protocol(adapter: BuggyStoreAdapter) -> None:
    assert isinstance(adapter, ManagedTargetAdapter)
    assert isinstance(adapter.observation_provider(), ObservationProvider)


@pytest.mark.adapters
def test_the_descriptor_advertises_both_scenario_modes(adapter: BuggyStoreAdapter) -> None:
    """FR-017: the panel shows a required selector with exactly these two."""
    assert adapter.descriptor.target_id == TARGET_ID
    assert adapter.descriptor.execution_mode is ExecutionMode.MANAGED
    assert adapter.descriptor.supported_scenario_modes == ("pre_fix", "post_fix")
    assert ADAPTER_ID == "integrations.buggy_store"


@pytest.mark.adapters
def test_the_five_appendix_d2_tools_are_published(adapter: BuggyStoreAdapter) -> None:
    assert [spec.name for spec in adapter.tool_specs()] == [
        "search_catalog",
        "get_cart",
        "update_cart",
        "apply_discount",
        "proceed_to_checkout",
    ]


@pytest.mark.adapters
@pytest.mark.parametrize("spec", TOOL_SPECS, ids=lambda spec: spec.name)
def test_every_tool_publishes_a_closed_input_schema(spec) -> None:
    """§11.4: "explicit JSON Schema with required properties, enums, descriptions"."""
    schema = spec.input_schema
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    for name, definition in schema.get("properties", {}).items():
        assert definition.get("description"), f"{spec.name}.{name} has no description"


@pytest.mark.adapters
def test_the_argument_enums_match_the_seeded_catalog(adapter: BuggyStoreAdapter) -> None:
    """A schema that allowed an unseeded product would fail at the store instead."""
    update = next(spec for spec in TOOL_SPECS if spec.name == "update_cart")
    assert update.input_schema["properties"]["product_id"]["enum"] == [
        "mug-ceramic-001",
        "notebook-001",
        "tote-001",
    ]
    discount = next(spec for spec in TOOL_SPECS if spec.name == "apply_discount")
    assert discount.input_schema["properties"]["code"]["enum"] == ["SAVE20"]


@pytest.mark.adapters
def test_side_effect_and_retry_semantics_match_the_specified_behaviour() -> None:
    """Read off Appendix D.2's closing paragraph rather than guessed."""
    by_name = {spec.name: spec for spec in TOOL_SPECS}

    assert by_name["get_cart"].side_effect is SideEffectClass.READ_ONLY
    assert by_name["get_cart"].retry is RetrySemantics.READ_ONLY_SAFE

    assert by_name["update_cart"].retry is RetrySemantics.IDEMPOTENT_BY_REQUEST_ID
    # No request ID in its schema, and a repeat is a successful no-op.
    assert by_name["apply_discount"].retry is RetrySemantics.NATURALLY_IDEMPOTENT
    assert "request_id" not in by_name["apply_discount"].input_schema["properties"]

    # Consent is what makes checkout protected rather than merely mutating.
    assert by_name["proceed_to_checkout"].side_effect is SideEffectClass.PROTECTED_MUTATING


# --- the effect map (§13.4, FR-015) -----------------------------------------


@pytest.mark.adapters
def test_the_effect_map_is_the_specs_table_verbatim(adapter: BuggyStoreAdapter) -> None:
    assert adapter.effect_map() == {
        "search_catalog": (),
        "get_cart": (),
        "update_cart": (
            "target.cart.items",
            "target.cart.subtotal",
            "target.cart.total",
        ),
        "apply_discount": ("target.cart.discount", "target.cart.total"),
        "proceed_to_checkout": ("target.order",),
    }


@pytest.mark.adapters
def test_the_effect_map_is_derived_from_the_tool_specs() -> None:
    """Two hand-written copies would eventually disagree about what a tool claims."""
    assert {
        spec.name: tuple(str(path) for path in spec.effect_paths) for spec in TOOL_SPECS
    } == EFFECT_MAP


@pytest.mark.adapters
def test_a_read_only_tool_declares_no_effects() -> None:
    """§13.4 lists both reads as "none; read-only"."""
    assert EFFECT_MAP["search_catalog"] == ()
    assert EFFECT_MAP["get_cart"] == ()


@pytest.mark.adapters
def test_the_discount_effect_covers_the_path_its_fault_fails_to_change() -> None:
    """Without this row, FR-055 cannot name apply_discount as the lying action."""
    assert "target.cart.total" in EFFECT_MAP["apply_discount"]


# --- allowlisting (FR-015, §20.2) -------------------------------------------


@pytest.mark.adapters
@pytest.mark.parametrize(
    "tool_name",
    ["delete_everything", "update_Cart", "", "checkout", None, 42],
)
async def test_a_tool_outside_the_allowlist_is_refused(
    adapter: BuggyStoreAdapter, tool_name: object
) -> None:
    """Refused before a request is formed, so an invented name reaches nothing."""
    with pytest.raises(ToolNotAllowed):
        await adapter.execute("ws-1", tool_name, {}, _context())  # type: ignore[arg-type]


@pytest.mark.adapters
def test_the_allowlist_is_exactly_the_published_specs() -> None:
    assert {spec.name for spec in TOOL_SPECS} == TOOL_NAMES


# --- prepare (§9.1, FR-018) -------------------------------------------------


@pytest.mark.adapters
async def test_prepare_selects_the_scenario_and_seeds_state(
    adapter: BuggyStoreAdapter,
) -> None:
    await _armed(adapter, "pre_fix", "discount_reported_but_not_applied")
    observation = await adapter.observation_provider().capture("ws-1")
    assert observation.payload["cart"]["items"] == {}


@pytest.mark.adapters
async def test_prepare_refuses_a_mode_the_adapter_never_advertised(
    adapter: BuggyStoreAdapter,
) -> None:
    """§9.1: the core validates the selection against the descriptor."""
    with pytest.raises(ContractError, match="not supported by target"):
        await adapter.prepare("ws-1", {}, ScenarioSelection(scenario_mode="external_current"))


@pytest.mark.adapters
async def test_prepare_refuses_a_fixture_it_cannot_honour(
    adapter: BuggyStoreAdapter,
) -> None:
    """Silently dropping a fixture would make an eval replay restore the wrong start."""
    with pytest.raises(ValueError, match="refusing to silently drop"):
        await adapter.prepare(
            "ws-1", {"cart": {"mug": 3}}, ScenarioSelection(scenario_mode="post_fix")
        )


@pytest.mark.adapters
async def test_prepare_resets_state_between_runs(adapter: BuggyStoreAdapter) -> None:
    await _armed(adapter)
    await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 2, "request_id": "req-1" * 3},
        _context(),
    )
    await _armed(adapter)
    observation = await adapter.observation_provider().capture("ws-1")
    assert observation.payload["cart"]["items"] == {}


# --- execute (§9.1, FR-032) -------------------------------------------------


@pytest.mark.adapters
async def test_a_read_only_call_returns_a_bounded_self_report(
    adapter: BuggyStoreAdapter,
) -> None:
    await _armed(adapter)
    result = await adapter.execute("ws-1", "search_catalog", {"query": "mug"}, _context())

    assert result.terminal_event is OutcomeEventType.TOOL_INVOCATION_COMPLETED
    assert result.reported_status is ToolReportedStatus.SUCCESS
    assert len(result.reported_summary) <= 1_500
    assert result.source_classification is EvidenceSourceClassification.TOOL_REPORTED


@pytest.mark.adapters
async def test_a_mutation_records_the_state_version_either_side(
    adapter: BuggyStoreAdapter,
) -> None:
    """FR-032: evidence that does not depend on the tool's own words."""
    await _armed(adapter)
    result = await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 1, "request_id": "req-000000000001"},
        _context(),
    )
    assert result.state_version_before == "1"
    assert result.state_version_after == "2"


@pytest.mark.adapters
async def test_a_repeated_request_reports_the_first_persisted_result(
    adapter: BuggyStoreAdapter,
) -> None:
    await _armed(adapter)
    arguments = {"product_id": MUG, "quantity": 1, "request_id": "req-000000000001"}
    first = await adapter.execute("ws-1", "update_cart", arguments, _context(1))
    repeat = await adapter.execute("ws-1", "update_cart", arguments, _context(2))

    assert repeat.reported_status is ToolReportedStatus.SUCCESS
    assert repeat.state_version_after == first.state_version_after


@pytest.mark.adapters
async def test_a_reapplied_discount_reports_already_applied(
    adapter: BuggyStoreAdapter,
) -> None:
    """App. D.2's fourth reported status, reaching the harness intact."""
    await _armed(adapter)
    await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 1, "request_id": "req-000000000001"},
        _context(1),
    )
    await adapter.execute("ws-1", "apply_discount", {"code": "SAVE20"}, _context(2))
    repeat = await adapter.execute("ws-1", "apply_discount", {"code": "SAVE20"}, _context(3))

    assert repeat.reported_status is ToolReportedStatus.ALREADY_APPLIED


@pytest.mark.adapters
async def test_a_store_refusal_becomes_a_failed_invocation(
    adapter: BuggyStoreAdapter,
) -> None:
    await _armed(adapter)
    result = await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": "bicycle-001", "quantity": 1, "request_id": "req-000000000001"},
        _context(),
    )
    assert result.terminal_event is OutcomeEventType.TOOL_INVOCATION_FAILED
    assert result.reported_status is None
    assert result.error_code == "product_not_found"


@pytest.mark.adapters
async def test_a_conflicting_retry_is_a_failure_not_a_silent_second_mutation(
    adapter: BuggyStoreAdapter,
) -> None:
    await _armed(adapter)
    await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 1, "request_id": "req-000000000001"},
        _context(1),
    )
    conflict = await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 4, "request_id": "req-000000000001"},
        _context(2),
    )
    assert conflict.terminal_event is OutcomeEventType.TOOL_INVOCATION_FAILED
    assert conflict.error_code == "idempotency_key_reused"


# --- safe blocks are not failures (FR-033) ----------------------------------


def _consented(sequence: int = 9) -> ExecutionContext:
    """A context carrying the harness's human approval for this invocation."""
    return _context(sequence).model_copy(update={"human_consent_granted": True})


def _refusing_store(status_code: int, body: dict) -> httpx.AsyncClient:
    """A client that answers the checkout call with one documented refusal.

    The adapter opens and approves the target's own confirmation inside a single
    dispatch, so a store that refuses at checkout is a *race* outcome — the
    record lapsed or was cancelled between approving it and spending it. Rare,
    but not dead: FR-033 says the adapter must report those as safe blocks
    rather than failures, and this is the only way to reach that translation
    deterministically.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/checkout"):
            return httpx.Response(status_code, json=body)
        if "confirmations" in request.url.path:
            return httpx.Response(201, json={"confirmation_id": "cnf-store-1"})
        return httpx.Response(200, json={"state_version": 1})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://buggy-store.test"
    )


@pytest.mark.adapters
async def test_an_approved_checkout_completes_successfully(
    adapter: BuggyStoreAdapter,
) -> None:
    """The harness states that a human approved; the adapter records that in the
    form this target requires and spends it (§14, FR-060)."""
    await _armed(adapter)

    result = await adapter.execute(
        "ws-1",
        "proceed_to_checkout",
        {"request_id": "req-000000000009"},
        _consented(),
    )
    assert result.terminal_event is OutcomeEventType.TOOL_INVOCATION_COMPLETED
    assert result.reported_status is ToolReportedStatus.SUCCESS
    assert "order_id=" in result.reported_summary


@pytest.mark.adapters
async def test_a_checkout_without_human_consent_creates_nothing(
    adapter: BuggyStoreAdapter,
) -> None:
    """Fails closed, and *before* the store is contacted.

    The constitution forbids an agent creating or approving its own consent, and
    this adapter is the last place that could be broken quietly. So the check is
    asserted twice: the call raises, and the store still holds no confirmation
    and no order — an adapter that opened one before noticing would leave a
    consent record nobody asked for.
    """
    await _armed(adapter)

    with pytest.raises(MissingHumanConsent):
        await adapter.execute(
            "ws-1",
            "proceed_to_checkout",
            {"request_id": "req-000000000009"},
            _context(9),  # no consent
        )

    cart = await adapter._client.get("/demo/api/v1/store/cart", headers={"X-Workspace-Id": "ws-1"})
    assert cart.json()["order"]["created"] is False


@pytest.mark.adapters
@pytest.mark.parametrize(
    ("lapsed", "reported"),
    [
        ("denied", ToolReportedStatus.BLOCKED_BY_USER),
        ("cancelled", ToolReportedStatus.BLOCKED_BY_USER),
        # A clock running out is not a person refusing, and the report says so.
        ("expired", ToolReportedStatus.BLOCKED_BY_EXPIRY),
    ],
)
async def test_a_store_refusal_after_consent_is_a_safe_block_not_a_failure(
    lapsed: str, reported: ToolReportedStatus
) -> None:
    """FR-033: a denied, expired or cancelled protected action is an expected
    terminal outcome, so the adapter reports a *completed* invocation with a
    blocked status.

    An adapter that returned a failure here would make the harness punish the
    safe behaviour it exists to encourage, and the consent policy would fail a
    run that did exactly the right thing.
    """
    refusal = {
        "error": {
            "code": "CONFIRMATION_REQUIRED",
            "message": f"confirmation is {lapsed}",
            "details": {"status": lapsed},
        }
    }
    async with _refusing_store(409, refusal) as client:
        adapter = BuggyStoreAdapter(client, clock=lambda: EPOCH)

        result = await adapter.execute(
            "ws-1", "proceed_to_checkout", {"request_id": "req-000000000009"}, _consented()
        )

    assert result.terminal_event is OutcomeEventType.TOOL_INVOCATION_COMPLETED
    assert result.reported_status is reported
    assert result.claims_success() is False


@pytest.mark.adapters
async def test_a_store_demanding_confirmation_is_a_failure_not_a_safe_block() -> None:
    """The distinction that keeps `blocked_by_user` meaningful.

    `confirmation_required` means nothing was safely refused — the store never
    saw an approval at all. Treating that as a safe block would let a run that
    skipped consent entirely look like one that respected a denial.
    """
    # The same code, with no `status` detail: the store never saw an approval,
    # so there is no safe refusal to report.
    unexplained = {"error": {"code": "CONFIRMATION_REQUIRED", "message": "approval required"}}
    async with _refusing_store(409, unexplained) as client:
        adapter = BuggyStoreAdapter(client, clock=lambda: EPOCH)

        result = await adapter.execute(
            "ws-1", "proceed_to_checkout", {"request_id": "req-000000000009"}, _consented()
        )

    assert result.terminal_event is OutcomeEventType.TOOL_INVOCATION_FAILED
    assert result.error_code == "confirmation_required"


# --- observe (§9.3, BUILD_ORDER M2) -----------------------------------------


@pytest.mark.adapters
async def test_the_observation_is_mounted_under_target_with_the_named_provider(
    adapter: BuggyStoreAdapter,
) -> None:
    """M2: "normalize target state under `target`, with provider buggy_store_state"."""
    await _armed(adapter)
    observation = await adapter.observation_provider().capture("ws-1")

    assert observation.namespace == "target"
    assert observation.provider_id == PROVIDER_ID == "buggy_store_state"
    assert observation.provenance == PROVENANCE


@pytest.mark.adapters
async def test_state_version_is_metadata_not_business_payload(
    adapter: BuggyStoreAdapter,
) -> None:
    """§9.3: it "remains observation metadata rather than business payload"."""
    await _armed(adapter)
    observation = await adapter.observation_provider().capture("ws-1")

    assert observation.state_version == "1"
    assert "state_version" not in observation.payload
    assert "state_version" not in observation.as_context()["target"]


@pytest.mark.adapters
async def test_contract_paths_resolve_through_the_observation(
    adapter: BuggyStoreAdapter,
) -> None:
    """The §10.1 contract's paths must resolve against what the provider returns."""
    from actionwitness_core.contracts.paths import ObservationPath, resolve

    await _armed(adapter)
    await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 1, "request_id": "req-000000000001"},
        _context(1),
    )
    await adapter.execute("ws-1", "apply_discount", {"code": "SAVE20"}, _context(2))
    context = (await adapter.observation_provider().capture("ws-1")).as_context()

    for path, expected in (
        ("target.cart.items.mug.quantity", 1),
        ("target.cart.total", "20.00"),
        ("target.order.created", False),
    ):
        resolution = resolve(ObservationPath.parse(path), context)
        assert resolution.found is True, path
        assert resolution.value == expected, path


@pytest.mark.adapters
async def test_the_observation_covers_preferences(adapter: BuggyStoreAdapter) -> None:
    """§13.2 puts them there so §12.16 has a path outside any cart contract."""
    await _armed(adapter)
    observation = await adapter.observation_provider().capture("ws-1")
    assert observation.payload["preferences"] == {"delivery_note": "", "gift_wrap": False}


@pytest.mark.adapters
async def test_an_observation_is_never_manufactured_from_a_partial_response(
    adapter: BuggyStoreAdapter,
) -> None:
    """Constitution §5: observation failure is explicit, never a degraded success."""
    provider = adapter.observation_provider()
    with pytest.raises(ValueError, match="must not be manufactured"):
        provider.normalize({"unexpected": True})


@pytest.mark.adapters
async def test_two_workspaces_observe_different_state(adapter: BuggyStoreAdapter) -> None:
    await _armed(adapter)
    await adapter.execute(
        "ws-1",
        "update_cart",
        {"product_id": MUG, "quantity": 2, "request_id": "req-000000000001"},
        _context(1),
    )
    other = await adapter.observation_provider().capture("ws-2")
    assert other.payload["cart"]["items"] == {}


# --- the boundary (BUILD_ORDER invariant 3) ---------------------------------


@pytest.mark.adapters
def test_the_adapter_imports_no_store_service_object() -> None:
    """Checked by module graph, so a lazy import in a handler cannot hide."""
    import ast
    from pathlib import Path

    import integrations.buggy_store as package

    forbidden = {
        "buggy_store.service",
        "buggy_store.repository",
        "buggy_store.models",
        "buggy_store.confirmations",
        "buggy_store.migrations",
    }
    for module in sorted(Path(package.__file__).parent.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        imported = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert imported & forbidden == set(), f"{module.name} imports a store service object"
        assert not any(name == "buggy_store" for name in imported), (
            f"{module.name} imports the store package directly"
        )


@pytest.mark.adapters
def test_the_adapter_reaches_the_store_only_through_its_versioned_api() -> None:
    """Every request path is under `/demo/api/v1` (§15.5)."""
    import re
    from pathlib import Path

    import integrations.buggy_store.adapter as adapter_module
    import integrations.buggy_store.observation as observation_module

    for module in (adapter_module, observation_module):
        source = Path(module.__file__).read_text(encoding="utf-8")
        for path in re.findall(r'"(/[a-z0-9/_{}.-]+)"', source):
            assert path.startswith("/demo/api/v1"), f"{module.__name__} reaches {path}"


@pytest.mark.adapters
def test_the_adapter_constructs_no_http_client() -> None:
    """ADR-0001: the composition root owns the client and its lifetime."""
    import ast
    from pathlib import Path

    import integrations.buggy_store.adapter as adapter_module

    tree = ast.parse(Path(adapter_module.__file__).read_text(encoding="utf-8"))
    constructed = {
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "httpx.AsyncClient" not in constructed
    assert "httpx.Client" not in constructed


# --- what the target says about itself (§23.1, AC-20) ------------------------


@pytest.mark.adapters
async def test_the_adapter_reports_the_scenario_it_prepared(adapter: BuggyStoreAdapter) -> None:
    """§23.1: `fault_active` is "derived by the adapter", so it must be askable.

    Asserted both ways round on the same adapter. A method that only ever
    returned `True` would pass a one-sided test and would record every run as
    faulty, which is the false claim this product exists to catch — pointed at
    itself.
    """
    await _armed(adapter, "pre_fix", "discount_reported_but_not_applied")
    running = await adapter.scenario_state("ws-1")

    await _armed(adapter, "post_fix", "discount_reported_but_not_applied")
    quiet = await adapter.scenario_state("ws-1")

    assert running.fault_active is True
    assert quiet.fault_active is False


@pytest.mark.adapters
async def test_a_selected_profile_alone_does_not_make_the_fault_active(
    adapter: BuggyStoreAdapter,
) -> None:
    """The distinction the field exists for.

    A `post_fix` comparison run carries the same `failure_profile` as its
    `pre_fix` pair — that is what makes the pair differ in one variable — and the
    fault is switched off. A reader who could not tell "recorded" from "running"
    would read the post-fix run as faulty.
    """
    await _armed(adapter, "post_fix", "discount_reported_but_not_applied")

    assert (await adapter.scenario_state("ws-1")).fault_active is False


@pytest.mark.adapters
async def test_no_fault_profile_is_never_reported_as_active(adapter: BuggyStoreAdapter) -> None:
    await _armed(adapter, "pre_fix", "none")

    assert (await adapter.scenario_state("ws-1")).fault_active is False


@pytest.mark.adapters
async def test_a_store_that_does_not_report_the_field_is_refused_not_defaulted() -> None:
    """An adapter response is untrusted input like any other (constitution §5).

    `False` would be read as "the target confirmed no defect is running", which
    is a different statement from "the target did not say". The second one has to
    raise, or a store that stopped reporting would silently relabel every faulty
    run as clean.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"scenario_mode": "pre_fix"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://buggy-store.test"
    ) as client:
        with pytest.raises(ValueError, match="fault_active"):
            await BuggyStoreAdapter(client).scenario_state("ws-1")


@pytest.mark.adapters
async def test_a_non_boolean_report_is_refused_too() -> None:
    """`"true"` is not `True`.

    A truthy string would sail through a `bool(...)` coercion and record an
    active fault on the strength of a type error.
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"fault_active": "true"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://buggy-store.test"
    ) as client:
        with pytest.raises(ValueError, match="fault_active"):
            await BuggyStoreAdapter(client).scenario_state("ws-1")
