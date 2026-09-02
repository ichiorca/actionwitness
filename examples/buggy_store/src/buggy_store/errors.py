"""Stable error codes for the store's public surface (spec v1.9 §15.5, §15.8).

§15.5 makes `/demo/api/v1` a public API that the store's own human UI and the
`integrations.buggy_store` adapter both call, so its error codes are a contract:
the adapter branches on them, and renaming one is a breaking change.

`retryable` is a safety statement, not a hint. It is true only when repeating the
identical request under its original request ID cannot duplicate a mutation
(constitution §5). `IDEMPOTENCY_KEY_REUSED` is therefore false: the caller sent
two different payloads under one key, and repeating either one is a guess about
which they meant.

The codes deliberately reuse the harness's spelling where the specification
already fixed one - `IDEMPOTENCY_KEY_REUSED` appears in §15.8's registry, and two
spellings of one condition would mean the adapter had to translate between them
for no reason. That is a shared *name*, not a shared module: the store imports
nothing from the assurance stack (BUILD_ORDER invariant 2).
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any

__all__ = [
    "ConfirmationRequired",
    "DiscountNotFound",
    "FaultProfileUnavailable",
    "IdempotencyConflict",
    "ProductNotFound",
    "StoreError",
    "StoreErrorCode",
    "UnexpectedStoreFailure",
    "ValidationFailed",
]


class StoreErrorCode(StrEnum):
    """The closed set of codes `/demo/api/v1` may return."""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    PRODUCT_NOT_FOUND = "PRODUCT_NOT_FOUND"
    DISCOUNT_NOT_FOUND = "DISCOUNT_NOT_FOUND"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_NOT_FOUND = "CONFIRMATION_NOT_FOUND"
    CONFIRMATION_NOT_APPROVED = "CONFIRMATION_NOT_APPROVED"
    FAULT_PROFILE_UNAVAILABLE = "FAULT_PROFILE_UNAVAILABLE"
    WORKSPACE_NOT_FOUND = "WORKSPACE_NOT_FOUND"
    STORE_ERROR = "STORE_ERROR"


class StoreError(Exception):
    """Base for every failure the store surfaces deliberately.

    Carries the HTTP status and retryability with the code, because the store
    owns its own API and there is no other layer to decide them. A bare
    exception escaping to a route handler would become a 500 with an internal
    message, which §15.8 forbids reaching a browser tool.
    """

    code: StoreErrorCode = StoreErrorCode.VALIDATION_FAILED
    http_status: int = 422
    retryable: bool = False

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: Mapping[str, Any] = dict(details or {})

    def as_envelope(self) -> dict[str, Any]:
        """The one wire shape every store error takes."""
        return {
            "error": {
                "code": str(self.code),
                "message": self.message,
                "retryable": self.retryable,
                "details": dict(self.details),
            }
        }


class ValidationFailed(StoreError):
    """A request did not satisfy the published schema."""

    code = StoreErrorCode.VALIDATION_FAILED
    http_status = 422


class ProductNotFound(StoreError):
    """A product ID outside the seeded catalog (§13.1)."""

    code = StoreErrorCode.PRODUCT_NOT_FOUND
    http_status = 404


class DiscountNotFound(StoreError):
    """A discount code outside the allowlist (Appendix D.2 enumerates it)."""

    code = StoreErrorCode.DISCOUNT_NOT_FOUND
    http_status = 404


class IdempotencyConflict(StoreError):
    """A request ID was reused with a different payload (Appendix D.2).

    Explicitly non-retryable: "reusing a request ID with a different payload
    returns `IDEMPOTENCY_KEY_REUSED`, `retryable: false`". Marking it retryable
    would invite the client to send one of the two payloads again and hope.
    """

    code = StoreErrorCode.IDEMPOTENCY_KEY_REUSED
    http_status = 409
    retryable = False

    def __init__(self, tool_name: str, request_id: str) -> None:
        super().__init__(
            f"request_id {request_id!r} was already used for {tool_name!r} with a "
            "different payload",
            details={"tool_name": tool_name, "request_id": request_id},
        )
        self.tool_name = tool_name
        self.request_id = request_id


class FaultProfileUnavailable(StoreError):
    """A recognised fault profile that this build does not implement (FR-011).

    Distinct from `VALIDATION_FAILED` on purpose. The name is real and the
    specification describes it, so refusing it as an unknown value would tell an
    operator the wrong thing; refusing it as unavailable tells them it exists and
    is not shipped yet. Either way it is never downgraded to `none`.
    """

    code = StoreErrorCode.FAULT_PROFILE_UNAVAILABLE
    http_status = 422


class ConfirmationRequired(StoreError):
    """A protected mutation was attempted without a valid approval (§14, FR-066)."""

    code = StoreErrorCode.CONFIRMATION_REQUIRED
    http_status = 409


class UnexpectedStoreFailure(StoreError):
    """The terminal mapping for a failure the store did not anticipate.

    Every other code in this module names a condition the store decided to
    refuse. This one names the absence of such a decision: something raised that
    no handler expected, and the alternative to giving it an envelope is
    FastAPI's default 500 body — a second wire shape, carrying whatever text the
    exception happened to hold. §15.8 forbids an internal detail reaching a
    browser tool, and an unanticipated exception is precisely the case where the
    message is most likely to name a table, a path, or a submitted value.

    So the message is fixed and `details` stays empty. Nothing about the cause
    is forwarded; the cause belongs in the server log, which is not the
    response. `retryable` is false because the store does not know whether the
    failure landed — an ambiguous outcome is never marked retryable
    (constitution §5).

    Project-allocated, mirroring the harness's `HARNESS_ERROR` in role. §15.8
    fixes no store-side spelling for this condition, and the two services keep
    separate vocabularies precisely so a reader can tell which one failed.
    """

    code = StoreErrorCode.STORE_ERROR
    http_status = 500
    retryable = False

    def __init__(self) -> None:
        super().__init__("The store could not complete the request.")
