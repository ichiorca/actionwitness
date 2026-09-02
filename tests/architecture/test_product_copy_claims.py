"""015-T7 — the claims the product copy is allowed to make.

Round-2 §5 sorts its own research into evidence tiers and says, in its heading,
"read this before quoting anything". §015's guardrail turns that into a rule:
"public breakage findings cited in product copy are *published third-party
reports*, never presented as this product's own scans."

That is a testable rule, and it needs to be tested rather than reviewed. Product
copy is the one artifact in this repository that gets rewritten by whoever is
enthusiastic that week, and the failure mode is not a bug that surfaces in a
stack trace — it is a sentence that overstates, ships, and is quoted back at the
project later.

So these tests hold three lines:

* the third-party findings are attributed and dated, so a reader can go and check;
* the counterweight — that no damage has been demonstrated — survives editing;
* the phrases that would turn someone else's report into our own scan, or a page
  count into a store count, cannot appear.

Deliberately mechanical. A test cannot decide whether copy is *honest*, but it
can decide whether the specific overstatements this project already identified
have crept back in, which is the failure that actually happens.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

WITNESS_COPY = REPO_ROOT / "docs" / "storefront-witness.md"
README = REPO_ROOT / "README.md"

#: Every file that speaks to a prospective user in the project's own voice.
#:
#: The research document is deliberately absent: it is a record of what was
#: found, tiers and unverified claims included, and holding it to the rules for
#: *published* copy would force it to delete the very caveats these tests exist
#: to preserve.
PRODUCT_COPY: tuple[Path, ...] = (WITNESS_COPY, README)

#: Turns of phrase that would restate someone else's report as our own work, or
#: inflate a figure the source itself qualified.
#:
#: Lowercased substrings rather than regexes: the check is for the specific
#: overstatements round-2 §5 already caught, not for a general theory of hype.
FORBIDDEN: tuple[tuple[str, str], ...] = (
    ("we scanned", "presents someone else's report as our own scan"),
    ("our scan of", "presents someone else's report as our own scan"),
    ("we tested allbirds", "claims first-hand testing of a store we do not own"),
    ("222,974 stores", "restates a page count as a store count"),
    ("hundreds of thousands of stores", "restates a page count as a store count"),
    ("2.4x", "quotes a dispute-rate figure its claimed source does not contain"),
    ("2.4×", "quotes a dispute-rate figure its claimed source does not contain"),
    ("webkit opposed", "repeats a claim round-2 §5 established is false"),
)


@pytest.mark.architecture
def test_the_product_copy_exists_to_be_checked() -> None:
    """The guard on everything below: a missing file would prove nothing."""
    missing = [path.name for path in PRODUCT_COPY if not path.is_file()]

    assert missing == [], f"product copy moved or was deleted: {missing}"


@pytest.mark.architecture
@pytest.mark.parametrize("path", PRODUCT_COPY, ids=lambda p: p.name)
def test_no_product_copy_restates_a_third_party_report_as_our_own(path: Path) -> None:
    """015's guardrail, held as a set of phrases rather than as an intention."""
    text = path.read_text(encoding="utf-8").lower()

    found = [f"{phrase!r} — {why}" for phrase, why in FORBIDDEN if phrase in text]

    assert found == [], f"{path.name} makes a claim the sources do not support:\n" + "\n".join(
        found
    )


@pytest.mark.architecture
def test_the_shopify_findings_are_attributed_and_dated() -> None:
    """A citation a reader cannot follow is an assertion.

    Both load-bearing findings carry their date and their nature — one is the
    vendor's own changelog, the other is an independent tester's published
    result — so a reader can weigh them separately and go and check.
    """
    text = WITNESS_COPY.read_text(encoding="utf-8")

    assert "published third-party reporting" in text
    assert "changelog" in text and "5 Aug 2026" in text
    assert "independent tester" in text and "6 Aug 2026" in text


@pytest.mark.architecture
def test_the_counterweight_survives_editing() -> None:
    """Round-2 §0: "do not claim damage that hasn't happened yet".

    The easiest sentence in the document to quietly drop, and the one whose
    absence would change what the product is claiming.
    """
    text = WITNESS_COPY.read_text(encoding="utf-8")

    assert "No damage is claimed." in text
    assert "near zero" in text, "the copy no longer states that live exposure is negligible"


@pytest.mark.architecture
def test_the_unverified_tier_is_labelled_rather_than_promoted() -> None:
    """§5's second tier is "worth spot-checking before public use".

    Repeating those findings is fine; repeating them *as verified* is not. The
    page-count caveat is asserted specifically because it is the one figure whose
    natural restatement is wrong.
    """
    text = WITNESS_COPY.read_text(encoding="utf-8")

    assert "not verified by us" in text
    assert "page count, not a store count" in text


@pytest.mark.architecture
def test_the_copy_does_not_promise_more_than_an_audit_delivers() -> None:
    """FR-163's limit, in the marketing voice rather than the report's.

    A page that sold a clean audit as a guarantee would undo the care taken in
    `audit_report.py`, where the same sentence is enforced word by word.
    """
    text = WITNESS_COPY.read_text(encoding="utf-8")

    assert "A clean audit is not a guarantee." in text
    assert "does not replace" in text or "does not score tool selection" in text
