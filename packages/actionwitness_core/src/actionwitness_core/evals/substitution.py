"""Deterministic stand-ins for redacted but replay-required values (§24.2 step 5).

Redaction happens at record time and replaces a sensitive value with the marker
`[REDACTED]` (§20.3). That is right for evidence a person reads and wrong for an
argument a replay must *send*: `"[REDACTED]"` is not an email address, so a
replayed call carrying it fails validation at the target and the case reproduces
an argument error instead of the regression it was cut from.

So a case substitutes a **type-valid** fixture. §24.2's own example is
`eval-user@example.invalid`, and the shape of that example carries the two rules
this module follows:

- **Deterministic.** The same marker in the same field yields the same
  substitute every time, or two generations of one case would differ and
  FR-080's byte-identical idempotence would fail on the one field nobody looks
  at.
- **Unmistakably fake.** `.invalid` is reserved by RFC 2606 precisely so it can
  never resolve. A plausible-looking substitute would be worse than the marker:
  somebody would eventually believe it.

Nothing here recovers the original value. There is none to recover — the source
was redacted before it was ever persisted (§20.3), and this module only sees the
marker. That is the point: a case is portable because it carries no secret, not
because its secrets are well hidden.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from actionwitness_core.kernel import JsonValue
from actionwitness_core.security.redaction import REDACTED

__all__ = ["SUBSTITUTES", "substitute_redacted"]

#: Field-name hints mapped to a type-valid, obviously fake stand-in. The keys
#: are matched as whole words against a field name, so `customer_email` and
#: `email` both resolve and `emailed_at` does not.
#:
#: Every value is in a reserved namespace (RFC 2606 `.invalid`, RFC 5737
#: documentation addresses) so none of them can ever reach a real system.
SUBSTITUTES: Mapping[str, str] = {
    "email": "eval-user@example.invalid",
    "mail": "eval-user@example.invalid",
    "token": "eval-token-000000000000",
    "secret": "eval-secret-000000000000",
    "password": "eval-password-000000",
    "authorization": "eval-authorization-0000",
    "key": "eval-key-000000000000",
    "phone": "+10000000000",
    "address": "1 Example Street",
    "name": "Eval Example",
    "ip": "192.0.2.1",
}

#: What a redacted value becomes when nothing more specific fits. Still
#: unmistakably fake, and still a string, which is the type a redacted scalar
#: had before the marker replaced it.
_FALLBACK = "eval-value-000000"


def substitute_redacted(value: JsonValue, *, field: str = "") -> JsonValue:
    """Replace every `[REDACTED]` marker with a deterministic type-valid stand-in.

    Walks containers so a marker nested inside an argument object is reached;
    non-string leaves are returned untouched, because redaction only ever
    replaces a value with a string and a number that survived was never
    sensitive.
    """
    if isinstance(value, Mapping):
        return {str(key): substitute_redacted(item, field=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        # The field name carries into elements: a list under `emails` should
        # substitute like an email, not like an unnamed value.
        return [substitute_redacted(item, field=field) for item in value]
    if isinstance(value, str) and value == REDACTED:
        return _substitute_for(field)
    return value


def _substitute_for(field: str) -> str:
    """The stand-in for one field name. Deterministic, and never a real value."""
    words = {word for word in _split(field) if word}
    for hint, replacement in SUBSTITUTES.items():
        if hint in words:
            return replacement
    return _FALLBACK


def _split(field: str) -> Sequence[str]:
    return field.lower().replace("-", "_").replace(".", "_").split("_")
