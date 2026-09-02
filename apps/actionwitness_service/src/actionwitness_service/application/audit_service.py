"""Authorized external-surface audits (§12.17, FR-160; 015-T1).

FR-160: "An external audit shall run only against an exact HTTPS origin the
operator supplies and explicitly asserts authorization for... Absent
authorization there is no audit." The origin "must additionally appear in a
deployment-configured allowlist".

**Two locks, and they are different locks.** The operator asserts authorization
for one origin; the deployment independently allows a set of origins. Either
alone would be wrong in a way the other prevents:

* An allowlist without an assertion means a deployment's configuration silently
  authorizes anybody who finds the workspace. §29.1 ships the public deployment
  with the module disabled precisely so "an anonymous visitor can never direct
  it at a third party" — but a configured deployment still needs a human to say
  *this* origin, *now*.
* An assertion without an allowlist means an anonymous visitor can point the
  harness at a stranger by typing a URL. That is the crawler this product
  refuses to be.

**The harness never contacts the audited origin** (FR-160a). Nothing in this
module makes an outbound request. Observations arrive from the operator's own
browser, already authenticated as itself, which is what removes the entire class
of server-side request-forgery risk that a server-side fetch would create. A
future maintainer looking for the HTTP client will not find one, and that is the
design rather than an omission.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from actionwitness_core.journeys.enums import EventActor, OutcomeEventType

from actionwitness_service.api.errors import ApiError, ApiErrorCode
from actionwitness_service.config import ExternalAuditSettings
from actionwitness_service.persistence.database import UnitOfWork
from actionwitness_service.persistence.repositories import new_id

__all__ = ["AuditService", "AuditStatus", "ExternalAudit", "normalize_origin"]


class AuditStatus(StrEnum):
    """§22's audit lifecycle, verbatim.

    `expired` is a terminal state of its own rather than a flavour of
    `completed`, because §22 is explicit that "expiry never converts an
    incomplete audit into a pass".
    """

    AUTHORIZED = "authorized"
    PAIRED = "paired"
    ENUMERATED = "enumerated"
    PACK_SELECTED = "pack_selected"
    RUNNING = "running"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    ERROR = "error"


#: The statuses §22 counts as finished. A workspace may hold at most one audit
#: outside this set, which the partial unique index in migration 5 enforces.
TERMINAL_STATUSES: Final[frozenset[AuditStatus]] = frozenset(
    {AuditStatus.COMPLETED, AuditStatus.EXPIRED, AuditStatus.CANCELLED, AuditStatus.ERROR}
)


class ExternalAudit:
    """One authorized audit, as the API reports it."""

    __slots__ = ("asserted_at", "asserted_by", "audit_id", "authorized_origin", "status")

    def __init__(self, row: Mapping[str, Any]) -> None:
        self.audit_id = str(row["id"])
        self.authorized_origin = str(row["authorized_origin"])
        self.asserted_by = str(row["authorization_asserted_by"])
        self.asserted_at = str(row["authorization_asserted_at"])
        self.status = AuditStatus(str(row["status"]))

    def as_document(self) -> dict[str, Any]:
        return {
            "audit_id": self.audit_id,
            "authorized_origin": self.authorized_origin,
            "authorization_asserted_by": self.asserted_by,
            "authorization_asserted_at": self.asserted_at,
            "status": self.status.value,
        }


def normalize_origin(value: str) -> str | None:
    """An exact HTTPS origin, or `None` when the value is not one.

    HTTPS only, no path, no query, no credentials, no trailing slash — the same
    shape `config._exact_origin` enforces on the allowlist, because the two are
    compared for equality and a value normalized by one rule and matched by
    another is how `https://shop.example` comes to match
    `https://shop.example.evil.test`.
    """
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(value.strip())
        hostname, port_number = parsed.hostname, parsed.port
    except ValueError:
        # The origin arrives in a request body, so "not an exact HTTPS origin"
        # has to cover the values that cannot be parsed at all — an unclosed
        # IPv6 authority raises from `urlsplit`, a non-numeric or out-of-range
        # port only when `.port` is read. `None` is what every other rejected
        # shape below returns, and the caller already turns it into
        # `AUDIT_NOT_AUTHORIZED`.
        return None
    if parsed.scheme != "https" or not hostname:
        return None
    if parsed.username or parsed.password:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return None
    port = f":{port_number}" if port_number else ""
    return f"https://{hostname.lower()}{port}"


class AuditService:
    """Records an authorization assertion and reads the audit it created."""

    def __init__(
        self,
        work: UnitOfWork,
        workspace_id: str,
        *,
        settings: ExternalAuditSettings | None,
    ) -> None:
        self._work = work
        self._workspace_id = workspace_id
        self._settings = settings

    async def assert_authorization(self, origin: str, *, actor: str) -> ExternalAudit:
        """FR-160's assertion, refused unless both locks open."""
        if self._settings is None:
            # §21.1: an absent module is a named unavailable state. The audit is
            # not merely unconfigured here — a deployment that never allowed an
            # origin has authorized nothing, so there is nothing to refuse
            # *specifically*, and saying so is more useful than a 404.
            raise ApiError(
                ApiErrorCode.AUDIT_NOT_AUTHORIZED,
                "External auditing is not enabled in this deployment.",
                details=[{"path": "origin", "message": "EXTERNAL_AUDIT_ENABLED is off"}],
            )

        normalized = normalize_origin(origin)
        if normalized is None:
            raise ApiError(
                ApiErrorCode.AUDIT_NOT_AUTHORIZED,
                "An audited origin must be an exact HTTPS origin with no path or credentials.",
                details=[{"path": "origin", "message": "not an exact https origin"}],
            )
        if normalized not in self._settings.allowed_origins:
            # Deliberately the same refusal as an unparseable origin. Telling a
            # caller "that origin is well-formed but not allowed" enumerates the
            # allowlist one guess at a time.
            raise ApiError(
                ApiErrorCode.AUDIT_NOT_AUTHORIZED,
                "This deployment has not allowed that origin for auditing.",
                details=[{"path": "origin", "message": "not in the deployment allowlist"}],
            )

        existing = await self.current()
        if existing is not None:
            raise ApiError(
                ApiErrorCode.RUN_IN_PROGRESS,
                "This workspace already has an audit in progress.",
                details=[{"path": "origin", "message": f"audit {existing.audit_id} is live"}],
            )

        asserted_at = self._work.now()
        audit_id = new_id("audit")
        await self._work.execute(
            """
            INSERT INTO external_audits (
                id, workspace_id, authorized_origin, authorization_asserted_by,
                authorization_asserted_at, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                self._workspace_id,
                normalized,
                actor,
                asserted_at,
                AuditStatus.AUTHORIZED.value,
                asserted_at,
            ),
        )
        loaded = await self.current()
        assert loaded is not None  # just inserted, inside this transaction
        return loaded

    async def append_authorization_event(self, run_id: str, audit: ExternalAudit) -> None:
        """FR-160's `audit_authorization_asserted`, once the audit has a run.

        The assertion is recorded *twice*, and the split is forced by the schema
        rather than chosen. `events.run_id` is `NOT NULL` — §17.1's timeline is
        run-scoped — while an authorization necessarily precedes any run, since
        FR-160 makes it the precondition for having one. So the durable record
        is the `external_audits` row, whose `authorization_asserted_by` and
        `authorization_asserted_at` columns §22 defines for exactly this, and the
        timeline gains the event as soon as there is a timeline to put it in.

        The alternative — widening `events.run_id` to nullable — is a table
        rebuild of the most-written table in the schema, and the constitution
        puts a destructive migration behind operator approval. Recorded in the
        015 deviations ledger.

        The event carries the *recorded* timestamp and actor, not the current
        ones: it describes when authorization was asserted, which is earlier than
        when this row is written.
        """
        await self._work.execute(
            """
            INSERT INTO events (
                id, run_id, sequence_number, event_type, actor, created_at,
                redacted_payload_json
            )
            SELECT ?, ?, COALESCE(MAX(sequence_number), 0) + 1, ?, ?, ?, ?
              FROM events WHERE run_id = ?
            """,
            (
                new_id("evt"),
                run_id,
                str(OutcomeEventType.AUDIT_AUTHORIZATION_ASSERTED.value),
                str(EventActor.HUMAN.value),
                audit.asserted_at,
                _payload(
                    audit.audit_id, audit.authorized_origin, audit.asserted_by, audit.asserted_at
                ),
                run_id,
            ),
        )

    async def complete(
        self, audit: ExternalAudit, *, pack_id: str, report_artifact_id: str
    ) -> ExternalAudit:
        """§22's terminal `completed`, carrying the report it produced.

        Written in the same transaction as the artifact row, so a workspace
        never holds a `completed` audit pointing at a report that is not there,
        and never holds a live audit whose report already exists — the second
        being the state that used to block the next audit until the workspace
        aged out.

        The status is re-read and re-checked rather than assumed from `audit`:
        the caller composed the report outside this transaction, and a status
        that moved in the meantime must not be overwritten by a decision taken
        before it moved.
        """
        current = await self.current()
        if current is None or current.audit_id != audit.audit_id:
            raise ApiError(
                ApiErrorCode.AUDIT_NOT_AUTHORIZED,
                "This audit is no longer the workspace's live audit.",
            )
        await self._work.execute(
            """
            UPDATE external_audits
               SET status = ?, contract_pack_id = ?, report_artifact_id = ?, completed_at = ?
             WHERE id = ? AND workspace_id = ?
            """,
            (
                AuditStatus.COMPLETED.value,
                pack_id,
                report_artifact_id,
                self._work.now(),
                audit.audit_id,
                self._workspace_id,
            ),
        )
        return ExternalAudit(
            {
                "id": audit.audit_id,
                "authorized_origin": audit.authorized_origin,
                "authorization_asserted_by": audit.asserted_by,
                "authorization_asserted_at": audit.asserted_at,
                "status": AuditStatus.COMPLETED.value,
            }
        )

    async def cancel(self) -> ExternalAudit:
        """§22's `cancelled` — the operator abandoning an audit they began.

        Added because the only ways out of `authorized` were completing and the
        24-hour workspace sweep: an audit started against the wrong origin, or
        begun and thought better of, held the workspace's one live-audit slot
        for a day. Terminal and one-way; a cancelled audit is not resumed, it is
        re-authorized, so nothing can quietly continue against an origin whose
        assertion the operator has withdrawn.
        """
        audit = await self.current()
        if audit is None:
            raise ApiError(
                ApiErrorCode.AUDIT_NOT_AUTHORIZED,
                "No authorized audit exists for this workspace.",
            )
        await self._work.execute(
            "UPDATE external_audits SET status = ?, completed_at = ? "
            "WHERE id = ? AND workspace_id = ?",
            (
                AuditStatus.CANCELLED.value,
                self._work.now(),
                audit.audit_id,
                self._workspace_id,
            ),
        )
        return ExternalAudit(
            {
                "id": audit.audit_id,
                "authorized_origin": audit.authorized_origin,
                "authorization_asserted_by": audit.asserted_by,
                "authorization_asserted_at": audit.asserted_at,
                "status": AuditStatus.CANCELLED.value,
            }
        )

    async def completed_report_artifact(self) -> tuple[str, str] | None:
        """The most recent completed audit's report artifact, with its origin.

        Reads the *completed* audit rather than the live one, because by the
        time a report exists the audit that produced it is terminal — a live
        audit is one that has not produced a report yet.
        """
        row = await self._work.fetch_one(
            """
            SELECT report_artifact_id, authorized_origin FROM external_audits
             WHERE workspace_id = ? AND status = ? AND report_artifact_id IS NOT NULL
             ORDER BY completed_at DESC, id DESC
             LIMIT 1
            """,
            (self._workspace_id, AuditStatus.COMPLETED.value),
        )
        if row is None:
            return None
        return str(row["report_artifact_id"]), str(row["authorized_origin"])

    async def current(self) -> ExternalAudit | None:
        """This workspace's live audit, if it has one."""
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        row = await self._work.fetch_one(
            f"""
            SELECT * FROM external_audits
             WHERE workspace_id = ? AND status NOT IN ({placeholders})
             ORDER BY created_at DESC
             LIMIT 1
            """,
            (self._workspace_id, *sorted(status.value for status in TERMINAL_STATUSES)),
        )
        return None if row is None else ExternalAudit(row)

    async def require_live(self) -> ExternalAudit:
        """The workspace's live audit, or the refusal that says there is none.

        The counterpart to `require_authorized_origin` for the steps that do not
        name a target. A submission carries no origin *by design* — §12.17 allows
        one asserted origin at a time and the audit already holds it, so asking
        the caller to name it again would create a second place where an origin
        could be introduced, and the only interesting thing a caller could do
        with that field is disagree with the assertion.
        """
        audit = await self.current()
        if audit is None:
            raise ApiError(
                ApiErrorCode.AUDIT_NOT_AUTHORIZED,
                "No authorized audit exists for this workspace.",
            )
        return audit

    async def require_authorized_origin(self, origin: str) -> ExternalAudit:
        """The gate every later step passes through (FR-160a).

        An observation is accepted only when its reported origin is *exactly*
        the one this workspace asserted. §12.17 forbids following "a redirect, a
        link, or a navigation beyond it", and equality is the only comparison
        that cannot be talked into a subdomain.
        """
        audit = await self.current()
        if audit is None:
            raise ApiError(
                ApiErrorCode.AUDIT_NOT_AUTHORIZED,
                "No authorized audit exists for this workspace.",
            )
        if normalize_origin(origin) != audit.authorized_origin:
            raise ApiError(
                ApiErrorCode.AUDIT_NOT_AUTHORIZED,
                "That observation did not come from the authorized origin.",
                details=[{"path": "origin", "message": "origin mismatch"}],
            )
        return audit


def _payload(audit_id: str, origin: str, actor: str, asserted_at: str) -> str:
    import json

    return json.dumps(
        {
            "audit_id": audit_id,
            "authorized_origin": origin,
            "asserted_by": actor,
            "asserted_at": asserted_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
