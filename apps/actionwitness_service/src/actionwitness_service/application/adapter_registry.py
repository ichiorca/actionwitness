"""The registry of target adapters (§21.1, §26.7, BUILD_ORDER invariant 12).

§26.7: "a missing adapter yields a clear unavailable state, not a process
failure." §21.1 goes further — the harness must start and run against a
non-commerce target with the Buggy Store package **absent from the environment
entirely**, not merely switched off.

Those two sentences decide the shape of this module:

**Registration is lazy and each entry is independent.** An adapter is built when
it is first asked for, and a failure to build it is recorded against that entry
alone. An eager registry that imported every integration at startup would let
one broken optional package take down a service that does not need it — the
"silent coupling this project claims to detect", as `config.py` puts it.

**`ImportError` is an expected outcome, not an exception to handle later.** The
Buggy Store integration is an optional distribution. `import integrations.
buggy_store` failing is precisely the §21.1 case, and it must produce the same
bounded unavailable state as a misconfigured base URL rather than a traceback.

**The reason is preserved.** `ModuleStatus` already distinguishes `disabled`
from `misconfigured` for exactly this: an operator who mistyped a store origin
needs to see a mistake, not an absence. A registry that reported both as "not
available" would turn a typo into a mystery.

Nothing here decides *policy*. Whether an adapter should be enabled is
`ServiceSettings`' answer, read once in the composition root; this module only
turns that answer plus the import result into an adapter or a reason.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.config import ModuleState, ModuleStatus, ServiceSettings

__all__ = ["AdapterRegistry", "AdapterSlot", "TargetUnavailable"]


class TargetUnavailable(ApiError):
    """A target was asked for and cannot be supplied.

    An `ApiError`, so a route that asks for an absent target produces §15.8's
    envelope with a reason an operator can act on, rather than a 500 whose text
    is a stack trace. `TARGET_UNAVAILABLE` is the specification's own code
    for this; nothing is invented here.
    """

    def __init__(self, name: str, reason: str) -> None:
        super().__init__(
            ApiErrorCode.TARGET_UNAVAILABLE,
            f"The {name} target is not available. {reason}",
        )
        self.target_name = name


@dataclass(frozen=True)
class AdapterSlot:
    """One target's availability and, when available, how to build its adapter.

    `factory` is `None` exactly when `state.is_enabled` is false, so the two
    cannot disagree — a slot that reported itself enabled with nothing to build
    would be a 500 waiting for the first request that used it.
    """

    name: str
    state: ModuleState
    factory: Callable[[], Any] | None = None

    @property
    def is_available(self) -> bool:
        return self.factory is not None


class AdapterRegistry:
    """Every optional target, each independently available or not."""

    def __init__(
        self, settings: ServiceSettings, *, client: httpx.AsyncClient | None = None
    ) -> None:
        """`client` is injected (ADR-0001), never constructed here.

        The composition root wires it to the configured base URL in production
        and to an `ASGITransport` in tests, and the adapter behaves identically
        either way. A registry that built its own client would make the test
        path a different path.
        """
        self._settings = settings
        self._client = client
        self._slots: dict[str, AdapterSlot] = {}
        self._register_all()

    # -- reading -------------------------------------------------------------

    def __iter__(self) -> Iterator[AdapterSlot]:
        return iter(self._slots.values())

    def __len__(self) -> int:
        return len(self._slots)

    def slot(self, name: str) -> AdapterSlot:
        try:
            return self._slots[name]
        except KeyError:
            raise TargetUnavailable(name, "No such target is registered.") from None

    def is_available(self, name: str) -> bool:
        return name in self._slots and self._slots[name].is_available

    def adapter(self, name: str) -> Any:
        """Build the adapter, or raise the bounded refusal explaining why not."""
        slot = self.slot(name)
        if slot.factory is None:
            raise TargetUnavailable(name, slot.state.reason)
        return slot.factory()

    def capability_report(self) -> Mapping[str, Mapping[str, str]]:
        """What §29.1's capability bar shows: every target, its status, its reason.

        Every registered target appears, available or not. A bar that listed only
        what worked would make a misconfiguration look like a feature that was
        never built.
        """
        return {
            slot.name: {"status": str(slot.state.status.value), "reason": slot.state.reason}
            for slot in self._slots.values()
        }

    def resolve(self, identifier: str | None) -> AdapterSlot | None:
        """Find a slot by module name **or** by the target id it advertises.

        Two vocabularies meet here and it is worth naming both. The registry is
        keyed by *module* (`buggy_store`, matching `ServiceSettings`), while a
        workspace and a contract name a *target* (`buggy-store`, from the
        adapter's descriptor). Accepting either is what lets FR-024 select a
        target from a contract's `target_id` without the caller having to know
        which of the two names it is holding.
        """
        if identifier is None:
            return None
        if identifier in self._slots:
            return self._slots[identifier]
        for slot in self._slots.values():
            if slot.factory is not None and slot.factory().descriptor.target_id == identifier:
                return slot
        return None

    def supported_scenario_modes(self, identifier: str | None) -> tuple[str, ...]:
        """What the named target advertises (§9.1).

        Read from the adapter's own descriptor rather than from a constant here,
        because the harness "validates the selected value against
        `TargetDescriptor.supported_scenario_modes` but neither interprets mode
        names nor implements a fault". An unavailable or unselected target
        advertises nothing, which makes every mode invalid rather than letting
        the check pass by default.
        """
        slot = self.resolve(identifier)
        if slot is None or slot.factory is None:
            return ()
        return tuple(slot.factory().descriptor.supported_scenario_modes)

    # -- registration --------------------------------------------------------

    def _register_all(self) -> None:
        """Each registration is independent and none may raise.

        Wrapped individually rather than in one `try`, so a failure in the first
        integration cannot skip the rest — which is the whole of §21.1's "one
        failed integration never disables the others".
        """
        self._register("buggy_store", self._build_buggy_store)

    def _register(self, name: str, build: Callable[[], Callable[[], Any]]) -> None:
        declared = self._settings.module(name)
        if not declared.is_enabled:
            # Configuration already decided. Nothing is imported, which is what
            # lets a deployment run with the package genuinely absent.
            self._slots[name] = AdapterSlot(name=name, state=declared)
            return

        try:
            factory = build()
        except ImportError as exc:
            # §21.1: the package may simply not be installed. That is an
            # expected deployment, not a failure to report as a fault.
            self._slots[name] = AdapterSlot(
                name=name,
                state=ModuleState(
                    name=name,
                    status=ModuleStatus.DISABLED,
                    reason=(
                        f"The {name} integration is not installed ({exc.name or 'import failed'})."
                    ),
                ),
            )
        except Exception as exc:
            self._slots[name] = AdapterSlot(
                name=name,
                state=ModuleState(
                    name=name,
                    status=ModuleStatus.MISCONFIGURED,
                    reason=f"The {name} integration could not be prepared: {type(exc).__name__}.",
                ),
            )
        else:
            self._slots[name] = AdapterSlot(name=name, state=declared, factory=factory)

    def _build_buggy_store(self) -> Callable[[], Any]:
        """Return a factory, so the adapter itself is built per use.

        The import happens here — inside `_register`'s guard — rather than at
        module scope, which is what makes a missing optional distribution a
        status rather than a crash at startup.
        """
        from integrations.buggy_store import BuggyStoreAdapter

        settings = self._settings.buggy_store
        if settings is None:  # pragma: no cover - `is_enabled` already implies it
            raise RuntimeError("buggy_store is enabled without settings")
        if self._client is None:
            raise RuntimeError("no HTTP client was injected for the buggy_store target")

        client = self._client
        return lambda: BuggyStoreAdapter(client)
