"""Screen generated variants before a human is asked to read them (FR-100).

Spec v1.9 FR-100 ("reject variants containing secrets or instructions to bypass
confirmation"), §12.11, §5's untrusted-input rails.

**This is a filter, not a proof.** Unlike the credential check in
`integrations.google_evals.live` — which has a precise signal, the configured
variable's own name — no rule here can be complete. Natural language has
unbounded ways to say "skip the confirmation", and a secret is only recognisable
by shape. So this module reduces what reaches a reviewer; **the human approval
in FR-100's next clause is the actual control**, and nothing here should be read
as making that approval a formality.

Saying so matters more than it might seem. A screen advertised as exhaustive
teaches a reviewer to skim, which removes the one check that can actually catch
a novel phrasing.

**Why screen before review at all, then?** Because a reviewer should never be
shown a variant that asks them to approve bypassing a safeguard. Reading such a
text is itself the attack: FR-100 puts screening first so the question is never
put to a person, and §5's rail — content from a model is data, never an
instruction — is what that sequence protects.

**Findings name what matched, never the matched value.** A finding is written to
a log and shown in a review UI; repeating a suspected secret there would copy it
into two more places.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Final

from actionwitness_core.benchmarks.intents import CandidateVariants
from actionwitness_core.kernel import ContractError, CoreErrorCode, CoreModel, JsonValue

__all__ = [
    "BYPASS_PHRASES",
    "ScreeningConcern",
    "ScreeningFinding",
    "require_screened",
    "screen_variants",
]


class ScreeningConcern(StrEnum):
    """Why a variant was held back. Two concerns, two different remedies:
    a secret must be rotated, a bypass instruction must be regenerated."""

    SECRET_MATERIAL = "secret_material"
    CONFIRMATION_BYPASS = "confirmation_bypass"


#: Shapes that are credentials in practice. Each is anchored on a recognisable
#: prefix or scheme rather than on entropy: an entropy heuristic would flag
#: order identifiers and content hashes, and a screen that cried wolf on
#: ordinary data would be switched off.
_SECRET_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("openai-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}")),
    ("bearer token", re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{12,}", re.IGNORECASE)),
    ("aws access key id", re.compile(r"\bAKIA[0-9A-Z]{12,}")),
    ("github token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "assigned credential",
        # `password = "…"`, `api_key: …` — an assignment to a credential-ish
        # name. The name is the signal; the value's shape is not.
        re.compile(
            r"\b(?:api[_\-]?key|secret|password|passwd|token|credential)s?\b\s*[:=]\s*\S{6,}",
            re.IGNORECASE,
        ),
    ),
)

#: Phrasings that ask for a safeguard to be skipped. Stated as a list rather
#: than inferred, so adding one is a decision a reviewer can see — and so the
#: list can be read aloud in a security review, which a regex cannot.
BYPASS_PHRASES: Final[tuple[str, ...]] = (
    "skip confirmation",
    "skip the confirmation",
    "without confirmation",
    "without confirming",
    "no confirmation",
    "bypass confirmation",
    "bypass the confirmation",
    "don't ask",
    "do not ask",
    "without asking",
    "no need to ask",
    "auto-approve",
    "auto approve",
    "approve it yourself",
    "approve on my behalf",
    "pre-approved",
    "already approved",
    "skip approval",
    "without approval",
    "without my approval",
    "ignore the confirmation",
    "ignore confirmation",
    "no prompt",
    "suppress the prompt",
)

_WORD_EDGE: Final = re.compile(r"[^a-z0-9]+")


def _normalised(text: str) -> str:
    """Lowercased with punctuation flattened to single spaces.

    So `Don't ask!` and `dont  ask` both match `don't ask` after the same
    flattening. Matching raw text would let a comma defeat the whole list.
    """
    return f" {_WORD_EDGE.sub(' ', text.casefold()).strip()} "


class ScreeningFinding(CoreModel):
    """One reason a variant was held back.

    Carries the variant's position and what kind of thing matched — never the
    matched text. A finding reaches a log and a review screen, and repeating a
    suspected secret would copy it into two more places.
    """

    index: int
    concern: ScreeningConcern
    marker: str

    def canonical_document(self) -> dict[str, JsonValue]:
        return {"index": self.index, "concern": self.concern.value, "marker": self.marker}


def screen_variants(
    candidates: CandidateVariants,
    *,
    extra_secret_markers: Iterable[str] = (),
) -> tuple[ScreeningFinding, ...]:
    """Every concern found, in order. An empty tuple means nothing *matched*.

    `extra_secret_markers` is how the service passes deployment knowledge the
    core cannot have — the configured credential variable's name, for instance.
    The core stays target-neutral and the caller supplies what it knows.
    """
    markers = tuple(marker.strip().casefold() for marker in extra_secret_markers if marker.strip())
    findings: list[ScreeningFinding] = []

    for index, variant in enumerate(candidates.variants):
        text = variant.text
        flattened = _normalised(text)

        for label, pattern in _SECRET_PATTERNS:
            if pattern.search(text):
                findings.append(
                    ScreeningFinding(
                        index=index, concern=ScreeningConcern.SECRET_MATERIAL, marker=label
                    )
                )
        for marker in markers:
            if marker in text.casefold():
                findings.append(
                    ScreeningFinding(
                        index=index,
                        concern=ScreeningConcern.SECRET_MATERIAL,
                        marker="configured credential name",
                    )
                )
        for phrase in BYPASS_PHRASES:
            if f" {_WORD_EDGE.sub(' ', phrase).strip()} " in flattened:
                findings.append(
                    ScreeningFinding(
                        index=index,
                        concern=ScreeningConcern.CONFIRMATION_BYPASS,
                        marker=phrase,
                    )
                )

    return tuple(findings)


def require_screened(
    candidates: CandidateVariants,
    *,
    extra_secret_markers: Iterable[str] = (),
) -> CandidateVariants:
    """The candidates, or a refusal naming every concern.

    Refuses the **whole set** rather than dropping the offending variants. A
    filtered set would go to a human as though it were what generation produced,
    and the reviewer would approve six variants believing they had seen the
    output — which is the same substitution FR-100's ceiling refuses elsewhere.
    """
    findings = screen_variants(candidates, extra_secret_markers=extra_secret_markers)
    if not findings:
        return candidates

    summary = ", ".join(
        f"variant {finding.index} ({finding.concern.value}: {finding.marker})"
        for finding in findings
    )
    raise ContractError(
        f"generated variants were held back before review: {summary}. Regenerate "
        "the set; if a secret matched, treat it as exposed and rotate it. This "
        "screen is a filter, not a guarantee — the human review that follows is "
        "the control (FR-100).",
        code=CoreErrorCode.CONTRACT_VALIDATION_FAILED,
    )


def screening_report(findings: Sequence[ScreeningFinding]) -> Mapping[str, JsonValue]:
    """A structured summary for a review surface or an audit record."""
    return {
        "clean": not findings,
        "findings": [finding.canonical_document() for finding in findings],
    }
