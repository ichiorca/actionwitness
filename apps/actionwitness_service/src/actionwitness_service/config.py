"""Feature flags and module configuration (spec v1.9 §29.1; BUILD_ORDER §7/M0).

The rule this module exists to enforce is BUILD_ORDER invariant 12: **optional
integrations fail closed and cannot prevent the credential-free Buggy Store path
from running.** Absent or invalid configuration disables *only* its own module and
produces setup guidance; it never raises at startup and never disables anything
else. A demo that dies because a Tier 3 store is unreachable would be exactly the
kind of silent coupling this project claims to detect.

Configuration is read from an **injected mapping**, not `os.environ` directly, so
every absence and misconfiguration combination is testable without mutating
process state. The composition root passes `os.environ` once.

Secrets never live here. `LiveEvaluatorSettings` records the *name* of the
credential variable, never its value: model credentials belong to the pinned
evaluator's own process environment (FR-099), and a settings object is dumped into
logs and health output where a value would leak.

Environment variables, with provenance:

| Variable | Module | Provenance |
|---|---|---|
| `HARNESS_ENV` | harness | project (FR-005) |
| `HARNESS_DATABASE_PATH` | harness | project |
| `HARNESS_ARTIFACT_ROOT` | harness | project |
| `HARNESS_STATIC_ROOT` | harness | project (§29.1 step 4) |
| `HARNESS_TRUSTED_PROXIES` | harness | project (§20.1) |
| `HARNESS_PUBLIC_ORIGIN` | shopify | spec §29.1 |
| `SHOPIFY_STORE_ORIGIN` | shopify | spec §29.1 |
| `SHOPIFY_TEST_VARIANT_ID` | shopify | spec §29.1 |
| `SHOPIFY_EXPECTED_CURRENCY` | shopify | spec §29.1 |
| `EXTERNAL_AUDIT_ENABLED` | external_audit | spec §29.1 |
| `EXTERNAL_AUDIT_ALLOWED_ORIGINS` | external_audit | spec §29.1 |
| `BUGGY_STORE_ENABLED` | buggy_store | project |
| `BUGGY_STORE_BASE_URL` | buggy_store | project (ADR-0001) |
| `EVALUATOR_IMPORT_ENABLED` | evaluator_import | project |
| `LIVE_EVALUATOR_ENABLED` | live_evaluator | project |
| `LIVE_EVALUATOR_PROVIDER` | live_evaluator | project |
| `LIVE_EVALUATOR_MODEL` | live_evaluator | project |
| `LIVE_EVALUATOR_CREDENTIAL_VAR` | live_evaluator | project |
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

__all__ = [
    "MODULE_NAMES",
    "SERVICE_CLOSED_ENUMS",
    "BuggyStoreSettings",
    "DeploymentEnvironment",
    "EvaluatorImportSettings",
    "ExternalAuditSettings",
    "HarnessSettings",
    "LiveEvaluatorSettings",
    "ModuleState",
    "ModuleStatus",
    "ServiceSettings",
    "ShopifySettings",
]

DEFAULT_BUGGY_STORE_BASE_URL = "http://127.0.0.1:8001"
DEFAULT_DATABASE_PATH = "actionwitness.sqlite3"
DEFAULT_ARTIFACT_ROOT = "artifacts"
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


class DeploymentEnvironment(StrEnum):
    """Where this process is running. Project-allocated (FR-005).

    FR-005 makes the cookie's `Secure` attribute conditional — "documented local
    HTTP development may omit only the `Secure` attribute" — so something has to
    say which case applies. It is a two-value enum rather than a boolean flag
    because `HARNESS_SECURE_COOKIES=false` in production would be a one-typo
    downgrade, whereas naming the environment makes the claim auditable.

    The default is `production`. An operator who forgets to set it gets the
    stricter cookie and a broken local login, which is the failure that gets
    noticed; the reverse is the failure that does not.
    """

    LOCAL = "local"
    PRODUCTION = "production"


DEPLOYMENT_ENVIRONMENT_DESCRIPTIONS: Mapping[DeploymentEnvironment, str] = {
    DeploymentEnvironment.LOCAL: (
        "Documented local HTTP development. The workspace cookie omits `Secure` "
        "and nothing else about it changes (FR-005)."
    ),
    DeploymentEnvironment.PRODUCTION: (
        "Any non-local deployment. The workspace cookie is `Secure`, `HttpOnly`, "
        "and `SameSite=Strict` (§20.1)."
    ),
}


class ModuleStatus(StrEnum):
    """Why a module is or is not available.

    `misconfigured` is deliberately distinct from `disabled`. An operator who
    mistyped a store origin needs to see a mistake, not an absence — collapsing
    the two is how a broken Tier 3 config gets mistaken for a deliberate cut.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    MISCONFIGURED = "misconfigured"


MODULE_STATUS_DESCRIPTIONS: Mapping[ModuleStatus, str] = {
    ModuleStatus.ENABLED: "Configured and available.",
    ModuleStatus.DISABLED: "Not configured, or explicitly switched off. Not an error.",
    ModuleStatus.MISCONFIGURED: (
        "Configuration was supplied but rejected. The module is off and the reason "
        "is shown as setup guidance; it never degrades into a partial enablement."
    ),
}

#: Registered for the shared name registry alongside the core's domain enums.
SERVICE_CLOSED_ENUMS: tuple[tuple[str, str, Mapping[Any, str]], ...] = (
    ("module_status", "spec §29.1", MODULE_STATUS_DESCRIPTIONS),
    ("deployment_environment", "project (FR-005)", DEPLOYMENT_ENVIRONMENT_DESCRIPTIONS),
)

#: Every optional module, in the order the capability bar reports them.
MODULE_NAMES: tuple[str, ...] = (
    "buggy_store",
    "evaluator_import",
    "live_evaluator",
    "shopify",
    "external_audit",
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class HarnessSettings(_Frozen):
    """The harness's own deployment settings, distinct from optional modules.

    Not a `ModuleState`: an optional integration may fail closed and leave the
    service running, but there is no service without these.
    """

    environment: DeploymentEnvironment = DeploymentEnvironment.PRODUCTION
    database_path: str = DEFAULT_DATABASE_PATH
    #: The operator's own origin, used to validate `Origin` on mutations
    #: (§20.1). `None` means the documented local case, where the request's own
    #: origin is compared instead — the harness is served same-origin, so a
    #: legitimate page's `Origin` equals what it is posting to.
    public_origin: str | None = None
    artifact_root: str = DEFAULT_ARTIFACT_ROOT
    #: Where the composed image put the two built frontends (§29.1 step 4).
    #: `None` in a source checkout, where Vite serves both UIs and proxies both
    #: APIs itself — a service that required a build directory could not be
    #: started with `uvicorn` during development.
    static_root: str | None = None
    #: Peers whose forwarding header may be believed (§20.1: "explicitly trusted
    #: platform proxy metadata; never trust an arbitrary client-supplied
    #: forwarding header"). Empty by default, so an unconfigured deployment
    #: ignores the header entirely and rate-limits each client as itself.
    trusted_proxies: frozenset[str] = frozenset()

    @property
    def secure_cookies(self) -> bool:
        """FR-005: `Secure` everywhere except documented local HTTP."""
        return self.environment is DeploymentEnvironment.PRODUCTION


class ModuleState(_Frozen):
    """One module's availability plus the guidance an operator can act on."""

    name: str
    status: ModuleStatus
    reason: str

    @property
    def is_enabled(self) -> bool:
        return self.status is ModuleStatus.ENABLED


class BuggyStoreSettings(_Frozen):
    """The deterministic demo target reached over its versioned HTTP API (ADR-0001)."""

    base_url: str


class EvaluatorImportSettings(_Frozen):
    """Tier 2 report import. Requires no model credential (spec §25.3)."""

    max_report_bytes: int = 1_048_576  # 1 MiB, enforced before parsing (FR-090)
    max_trials: int = 100


class LiveEvaluatorSettings(_Frozen):
    """Tier 3 live benchmark. Holds the credential's *name*, never its value."""

    provider: str
    model: str
    credential_var: str


class ShopifySettings(_Frozen):
    """Tier 3 authorized development store. One exact origin, cart-only."""

    harness_public_origin: str
    store_origin: str
    test_variant_id: str
    expected_currency: str


class ExternalAuditSettings(_Frozen):
    """Tier 3 authorized external-surface audit. Allowlist is exact HTTPS origins."""

    allowed_origins: tuple[str, ...]


# --- parsing helpers --------------------------------------------------------


def _flag(environ: Mapping[str, str], key: str, *, default: bool) -> bool | None:
    """Tri-state: True, False, or None when the value is not a recognised boolean."""
    raw = environ.get(key)
    if raw is None:
        return default
    lowered = raw.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    return None


def _exact_origin(value: str, *, require_https: bool) -> str:
    """Return a normalized `scheme://host[:port]`, or raise with a usable reason.

    "Exact origin" means no path, query, fragment, credentials, or trailing
    slash. A permissive parse here is a security hole: the Shopify CORS rule and
    the audit allowlist both compare origins by equality, and `https://store.example/`
    matching `https://store.example.evil/` is precisely the failure to avoid.
    """
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{value!r} must use http or https")
    if require_https and parsed.scheme != "https":
        raise ValueError(f"{value!r} must use https")
    if not parsed.hostname:
        raise ValueError(f"{value!r} names no host")
    if parsed.username or parsed.password:
        raise ValueError(f"{value!r} must not embed credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(f"{value!r} must be a bare origin with no path, query, or fragment")
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


def _base_url(value: str) -> str:
    """An origin with an optional path prefix, e.g. `http://host:8001/demo/api/v1`."""
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"{value!r} must use http or https")
    if not parsed.hostname:
        raise ValueError(f"{value!r} names no host")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{value!r} must not carry a query or fragment")
    return value.strip().rstrip("/")


def _disabled(name: str, reason: str) -> ModuleState:
    return ModuleState(name=name, status=ModuleStatus.DISABLED, reason=reason)


def _misconfigured(name: str, reason: str) -> ModuleState:
    return ModuleState(name=name, status=ModuleStatus.MISCONFIGURED, reason=reason)


def _enabled(name: str, reason: str) -> ModuleState:
    return ModuleState(name=name, status=ModuleStatus.ENABLED, reason=reason)


# --- per-module resolution --------------------------------------------------


def _resolve_harness(environ: Mapping[str, str]) -> HarnessSettings:
    """Never raises: an unrecognised environment falls back to the strict one.

    Every other resolver here may report `misconfigured` and switch its module
    off, because an optional integration that fails closed leaves a working
    service. This one has nothing to switch off, so a bad value resolves to
    `production` — the choice that makes the cookie stricter, not looser.
    """
    raw = environ.get("HARNESS_ENV", "").strip().lower()
    environment = (
        DeploymentEnvironment.LOCAL
        if raw == DeploymentEnvironment.LOCAL
        else DeploymentEnvironment.PRODUCTION
    )
    database_path = environ.get("HARNESS_DATABASE_PATH", "").strip() or DEFAULT_DATABASE_PATH

    # Shared with the Shopify module, which requires it; here it is optional.
    # A value that will not parse is dropped rather than accepted loosely: a
    # half-parsed origin compared by equality refuses everything, which is the
    # safe direction, and the fallback below is the documented local rule.
    raw_origin = environ.get("HARNESS_PUBLIC_ORIGIN", "").strip()
    try:
        public_origin = _exact_origin(raw_origin, require_https=False) if raw_origin else None
    except ValueError:
        public_origin = None

    artifact_root = environ.get("HARNESS_ARTIFACT_ROOT", "").strip() or DEFAULT_ARTIFACT_ROOT
    static_root = environ.get("HARNESS_STATIC_ROOT", "").strip() or None
    trusted_proxies = frozenset(
        part.strip()
        for part in environ.get("HARNESS_TRUSTED_PROXIES", "").split(",")
        if part.strip()
    )

    return HarnessSettings(
        environment=environment,
        database_path=database_path,
        public_origin=public_origin,
        artifact_root=artifact_root,
        static_root=static_root,
        trusted_proxies=trusted_proxies,
    )


def _resolve_buggy_store(
    environ: Mapping[str, str],
) -> tuple[BuggyStoreSettings | None, ModuleState]:
    name = "buggy_store"
    flag = _flag(environ, "BUGGY_STORE_ENABLED", default=True)
    if flag is None:
        return None, _misconfigured(name, "BUGGY_STORE_ENABLED is not a boolean.")
    if not flag:
        return None, _disabled(name, "BUGGY_STORE_ENABLED is off.")
    try:
        base_url = _base_url(environ.get("BUGGY_STORE_BASE_URL", DEFAULT_BUGGY_STORE_BASE_URL))
    except ValueError as exc:
        return None, _misconfigured(name, f"BUGGY_STORE_BASE_URL invalid: {exc}")
    return BuggyStoreSettings(base_url=base_url), _enabled(name, f"Target API at {base_url}.")


def _resolve_evaluator_import(
    environ: Mapping[str, str],
) -> tuple[EvaluatorImportSettings | None, ModuleState]:
    name = "evaluator_import"
    flag = _flag(environ, "EVALUATOR_IMPORT_ENABLED", default=True)
    if flag is None:
        return None, _misconfigured(name, "EVALUATOR_IMPORT_ENABLED is not a boolean.")
    if not flag:
        return None, _disabled(name, "EVALUATOR_IMPORT_ENABLED is off.")
    # On by default and unconditionally: importing a checked-in report needs no
    # credential and no network (spec §25.3), so there is nothing to fail on.
    return EvaluatorImportSettings(), _enabled(name, "Report import available; no credential used.")


def _resolve_live_evaluator(
    environ: Mapping[str, str],
) -> tuple[LiveEvaluatorSettings | None, ModuleState]:
    name = "live_evaluator"
    flag = _flag(environ, "LIVE_EVALUATOR_ENABLED", default=False)
    if flag is None:
        return None, _misconfigured(name, "LIVE_EVALUATOR_ENABLED is not a boolean.")
    if not flag:
        return None, _disabled(name, "LIVE_EVALUATOR_ENABLED is off; recorded fixtures are used.")

    missing = [
        key
        for key in (
            "LIVE_EVALUATOR_PROVIDER",
            "LIVE_EVALUATOR_MODEL",
            "LIVE_EVALUATOR_CREDENTIAL_VAR",
        )
        if not environ.get(key, "").strip()
    ]
    if missing:
        return None, _misconfigured(name, f"missing {', '.join(missing)}.")

    credential_var = environ["LIVE_EVALUATOR_CREDENTIAL_VAR"].strip()
    if not environ.get(credential_var, "").strip():
        # Named but unset: the evaluator subprocess would fail at run time, so
        # report it now instead of at the moment of the demo.
        return None, _misconfigured(
            name, f"{credential_var} is named but not set in the environment."
        )

    return (
        LiveEvaluatorSettings(
            provider=environ["LIVE_EVALUATOR_PROVIDER"].strip(),
            model=environ["LIVE_EVALUATOR_MODEL"].strip(),
            credential_var=credential_var,
        ),
        _enabled(
            name, f"Credential supplied via {credential_var}; value stays in the environment."
        ),
    )


def _resolve_shopify(environ: Mapping[str, str]) -> tuple[ShopifySettings | None, ModuleState]:
    name = "shopify"
    required = (
        "HARNESS_PUBLIC_ORIGIN",
        "SHOPIFY_STORE_ORIGIN",
        "SHOPIFY_TEST_VARIANT_ID",
        "SHOPIFY_EXPECTED_CURRENCY",
    )
    supplied = [key for key in required if environ.get(key, "").strip()]
    if not supplied:
        return None, _disabled(name, "No Shopify development store configured.")
    missing = [key for key in required if key not in supplied]
    if missing:
        return None, _misconfigured(name, f"missing {', '.join(missing)}.")

    try:
        harness_origin = _exact_origin(environ["HARNESS_PUBLIC_ORIGIN"], require_https=False)
        store_origin = _exact_origin(environ["SHOPIFY_STORE_ORIGIN"], require_https=True)
    except ValueError as exc:
        return None, _misconfigured(name, f"invalid origin: {exc}")

    currency = environ["SHOPIFY_EXPECTED_CURRENCY"].strip()
    if not _CURRENCY.match(currency):
        return None, _misconfigured(name, f"{currency!r} is not a three-letter currency code.")

    return (
        ShopifySettings(
            harness_public_origin=harness_origin,
            store_origin=store_origin,
            test_variant_id=environ["SHOPIFY_TEST_VARIANT_ID"].strip(),
            expected_currency=currency,
        ),
        _enabled(name, f"Cart-only proof against {store_origin}."),
    )


def _resolve_external_audit(
    environ: Mapping[str, str],
) -> tuple[ExternalAuditSettings | None, ModuleState]:
    name = "external_audit"
    flag = _flag(environ, "EXTERNAL_AUDIT_ENABLED", default=False)
    if flag is None:
        return None, _misconfigured(name, "EXTERNAL_AUDIT_ENABLED is not a boolean.")
    if not flag:
        # Off unless explicitly enabled, so a public deployment can never let an
        # anonymous workspace assert authorization for an unconfigured origin.
        return None, _disabled(name, "EXTERNAL_AUDIT_ENABLED is off.")

    raw = environ.get("EXTERNAL_AUDIT_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return None, _misconfigured(name, "EXTERNAL_AUDIT_ALLOWED_ORIGINS is empty.")

    origins: list[str] = []
    for candidate in (part.strip() for part in raw.split(",")):
        if not candidate:
            continue
        try:
            origins.append(_exact_origin(candidate, require_https=True))
        except ValueError as exc:
            return None, _misconfigured(name, f"invalid allowlist entry: {exc}")
    if not origins:
        return None, _misconfigured(name, "EXTERNAL_AUDIT_ALLOWED_ORIGINS lists no origin.")

    return (
        ExternalAuditSettings(allowed_origins=tuple(dict.fromkeys(origins))),
        _enabled(name, f"Allowlist holds {len(origins)} exact origin(s)."),
    )


class ServiceSettings(_Frozen):
    """Resolved configuration. Construction never raises on bad input.

    A module that could not be configured is absent here and carries a reason in
    `modules`; nothing else is affected.
    """

    harness: HarnessSettings = HarnessSettings()
    buggy_store: BuggyStoreSettings | None = None
    evaluator_import: EvaluatorImportSettings | None = None
    live_evaluator: LiveEvaluatorSettings | None = None
    shopify: ShopifySettings | None = None
    external_audit: ExternalAuditSettings | None = None
    modules: tuple[ModuleState, ...] = ()

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> ServiceSettings:
        harness = _resolve_harness(environ)
        buggy_store, buggy_store_state = _resolve_buggy_store(environ)
        evaluator_import, evaluator_import_state = _resolve_evaluator_import(environ)
        live_evaluator, live_evaluator_state = _resolve_live_evaluator(environ)
        shopify, shopify_state = _resolve_shopify(environ)
        external_audit, external_audit_state = _resolve_external_audit(environ)
        return cls(
            harness=harness,
            buggy_store=buggy_store,
            evaluator_import=evaluator_import,
            live_evaluator=live_evaluator,
            shopify=shopify,
            external_audit=external_audit,
            modules=(
                buggy_store_state,
                evaluator_import_state,
                live_evaluator_state,
                shopify_state,
                external_audit_state,
            ),
        )

    def module(self, name: str) -> ModuleState:
        for state in self.modules:
            if state.name == name:
                return state
        raise KeyError(f"unknown module {name!r}")

    def is_enabled(self, name: str) -> bool:
        return self.module(name).is_enabled
