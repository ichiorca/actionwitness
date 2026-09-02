"""One audit pass, from submitted browser evidence to the merchant report.

This is the step §12.17 always described and the service never had: authorization
existed, the classifier existed, the report composer existed, and nothing joined
them, so an operator could authorize an audit and then had nothing to call.

**The browser gathers; the server judges.** Nothing here contacts the audited
origin, and that is the property that keeps this feature clear of server-side
request forgery — the only party talking to the audited site is the person who
already has an account there. `getTools()` runs in their browser, the tools are
exercised in their browser, and `GET /cart.js` is read in their own session
(§25.8). What arrives here is the transcript, and this module's job is to refuse
to take any of it on trust.

**The pack is the operator's choice, never this module's.** FR-161 says a pack
"shall be offered" and "the operator selects it explicitly", and `match_pack`
returns every match rather than picking one for exactly that reason. So a
submission names its pack, and all this module does is check that the named pack
is one the enumerated surface actually supports — choosing on the operator's
behalf would decide, against a storefront somebody depends on, whether a write
path gets exercised.

**A tool's report is never promoted to an observation.** The submitted `reports`
and the submitted `cart.js` payloads travel through different parameters into
different arguments, and the payloads are normalized by the target adapter,
which checks their provenance rather than recording it. A submission that
labelled a tool result as a session read would be refused by the adapter, not
believed by this module.

**Absent observation stays absent.** When the browser could not read an
independent channel, the payloads arrive as `None` and every exercised tool is
classified `unobserved` — §12.17's `observation_unavailable`. Constitution §5:
"we could not check" must never round up to "it is fine".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.application.audit_evidence import AuditFinding, audit_findings
from actionwitness_service.application.audit_report import compose_audit_report

__all__ = ["AuditPass", "offered_packs", "run_audit_pass"]

#: The stored report's schema version, so a reader can tell which shape they
#: have without inferring it from the keys present.
AUDIT_REPORT_SCHEMA_VERSION = "1.0"

#: `artifacts.artifact_type` for a composed audit report.
AUDIT_REPORT = "audit_report"


@dataclass(frozen=True, slots=True)
class AuditPass:
    """What one pass concluded, before any of it is stored."""

    pack_id: str
    pack_title: str
    findings: tuple[AuditFinding, ...]
    report: dict[str, Any]


def offered_packs() -> list[dict[str, Any]]:
    """Every built-in pack, as a catalogue a client can offer from (FR-161).

    Static, and deliberately not a function of any submitted surface: a client
    holds its own `getTools()` result and can decide locally which packs a
    surface satisfies, so nothing here needs a request carrying a list of tools
    or origins. The catalogue is the same for every caller, which is what makes
    it uninteresting to an attacker and useless as a scanning affordance.
    """
    from integrations.shopify.pack import AUDIT_PACKS

    return [
        {
            "pack_id": pack.pack_id,
            "title": pack.title,
            # What a surface must publish for this pack to apply. The client
            # compares it against its own enumeration; the server re-checks the
            # same rule when the pass is submitted.
            "signature": list(pack.signature),
            # FR-162: present-but-never-invoked. Named in the catalogue so an
            # operator sees, before choosing, that the pack reports these
            # without exercising them.
            "never_invoked": list(pack.never_invoked),
        }
        for pack in AUDIT_PACKS
    ]


def run_audit_pass(
    *,
    authorized_origin: str,
    pack_id: str,
    enumerated: Sequence[str],
    reports: Mapping[str, Mapping[str, Any]],
    observed_before: Mapping[str, Any] | None,
    observed_after: Mapping[str, Any] | None,
) -> AuditPass:
    """Classify one submitted surface against one operator-selected pack.

    Pure: no I/O, no clock, no database. Everything it decides is a function of
    what it was given, which is what lets the same inputs be replayed into the
    same report.
    """
    from integrations.shopify.audit import PROVENANCE, AuditObservationError, ExternalAuditAdapter
    from integrations.shopify.pack import match_pack, pack_for

    pack = pack_for(pack_id)
    if pack is None:
        raise ApiError(
            ApiErrorCode.RESOURCE_NOT_FOUND,
            "No such contract pack.",
            details=[{"path": "pack_id", "message": "unknown pack"}],
        )
    if pack not in match_pack(enumerated):
        # The operator picked a pack this surface cannot support. Refused rather
        # than run against the tools that *are* present: a cart pack applied to
        # a surface with no cart tool would report the cart tool "absent" and
        # read as a finding about the storefront, when it is a finding about the
        # selection.
        raise ApiError(
            ApiErrorCode.PRECONDITION_FAILED,
            "That pack does not match the tools this surface publishes.",
            details=[{"path": "pack_id", "message": "surface does not satisfy the pack signature"}],
        )

    adapter = ExternalAuditAdapter(authorized_origin)
    try:
        before = (
            None
            if observed_before is None
            else adapter.normalize(dict(observed_before), PROVENANCE).payload
        )
        after = (
            None
            if observed_after is None
            else adapter.normalize(dict(observed_after), PROVENANCE).payload
        )
    except AuditObservationError as rejected:
        # 422: the payload cannot be made acceptable by retrying it unchanged,
        # and the caller needs to know it is theirs to fix. Never degraded into
        # "no observation" — a malformed read and an absent channel are
        # different facts, and silently converting one into the other would let
        # a broken submission be reported as an unobservable storefront.
        raise ApiError(ApiErrorCode.CONTRACT_VALIDATION_FAILED, str(rejected)) from rejected

    findings = audit_findings(
        enumerated=list(enumerated),
        expected=list(pack.document["expected_tools"]["calls"]),
        reports=reports,
        observed_before=before,
        observed_after=after,
        never_invoked=pack.never_invoked,
    )
    return AuditPass(
        pack_id=pack.pack_id,
        pack_title=pack.title,
        findings=findings,
        report=compose_audit_report(
            authorized_origin=authorized_origin,
            pack_id=pack.pack_id,
            pack_title=pack.title,
            findings=findings,
        ),
    )
