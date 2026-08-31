# ADR-0004 — RFC 8785 canonicalization implementation

- **Status:** Accepted
- **Date:** 2026-08-31
- **Implementing change:** 001-T4 (record + vector corpus); `actionwitness_core.security` in M1

## Context

Evidence records are hash-linked, and a hash is only as stable as the bytes fed to
it. Two structurally identical observations that serialize differently produce
different hashes, which would report evidence-chain corruption where none exists;
two *different* observations that serialize identically would hide a real change.
Canonicalization is therefore load-bearing for the product's central claim, not a
serialization detail.

RFC 8785 (JSON Canonicalization Scheme) is the chosen scheme. BUILD_ORDER §6
requires this decision before M1 immutable records and leaves the implementation
open: "a small target-neutral implementation or a narrowly implemented/tested
helper," which "must pass published vectors plus repository fixtures and reject
non-finite numbers."

The constraint that shapes the answer is where the code may live.
`actionwitness_core` owns canonical serialization (constitution §3 primitives
table) and currently depends on Pydantic alone. Every dependency added to the core
is inherited by anyone installing `actionwitness-core` standalone, which spec
§26.7 requires to work with every other package absent.

## Decision

Implement RFC 8785 as a **narrow, dependency-free helper in
`actionwitness_core.security`**, judged against the committed vector corpus at
`tests/fixtures/canonicalization/rfc8785_vectors.json`. Hashing stays `hashlib`
SHA-256 over the canonical UTF-8 bytes; this project writes no cryptography.

The helper's contract:

- **Output** is UTF-8 bytes with no insignificant whitespace. Hashing consumes the
  bytes, never a re-decoded string.
- **Object members** sort by UTF-16 code unit (§3.2.3). Python's `sorted()` sorts
  by code point, and the two orders differ whenever an astral key meets a BMP key
  above U+D800 — so the sort key is explicit, and the corpus contains a vector
  that fails a code-point implementation.
- **Numbers** follow ECMAScript `Number::toString` (§3.2.2.3): shortest
  round-tripping digits, exponential form only at magnitude ≥ 1e21 or < 1e-6, no
  zero-padded exponent, and `-0` emitted as `0`.
- **Strings** escape only `"`, `\` and C0 controls, using the `\b \f \n \r \t`
  shorthands and lowercase `\uXXXX` otherwise. Solidus is not escaped and
  non-ASCII is emitted literally.
- **Non-finite numbers are rejected** with a structured error. NaN and ±Infinity
  have no JSON form; encoding them, or coercing them to `null`, would let a broken
  observation hash successfully.
- **Integers outside ±(2^53 − 1) are rejected.** RFC 8785 numbers are IEEE-754
  doubles, so a larger integer cannot round-trip and would canonicalize to a
  neighbouring value. Identifiers and hashes are strings in this project's models
  precisely so they never reach this path; rejection makes a future violation
  loud rather than silently lossy.
- **Lone surrogates and non-JSON types are rejected.** Money never arrives as a
  float: exact `Decimal` values are carried as decimal strings, so the
  canonicalizer never sees one and no float rounding can touch a total.

Rejections raise a structured core error, never a bare `ValueError`, and never
degrade to a best-effort encoding.

### Verification

The committed corpus is the always-available floor: eight accept vectors carrying
input/canonical-text pairs plus three non-finite reject vectors, split into
`published` (RFC 8785's own worked example) and `repository` (this project's
shapes and known failure modes) origins as AC-1 requires.

`tests/unit/test_canonicalization_vectors.py` validates the corpus itself —
round-trip, member ordering, absence of whitespace, and that the ordering vector
still discriminates UTF-16 from code-point order — so a corpus typo fails the
build instead of a correct implementation.

## Consequences

### Positive

- The core keeps a one-dependency footprint, so the standalone-install gate stays
  trivially satisfiable and no license or advisory review is inherited by every
  downstream consumer for roughly a hundred lines of fully specified behavior.
- The rejection rules turn three silent-corruption paths — non-finite numbers,
  precision-losing integers, lone surrogates — into explicit failures, which is
  what an evidence chain needs.
- The corpus is data, not code: it is versioned, diffable, reusable by a future
  non-Python consumer, and it self-validates.
- Determinism is testable directly. Identical inputs must produce byte-identical
  output, which the M1 gate asserts.

### Negative

- **This reimplements a primitive**, which the constitution otherwise discourages,
  and the number rule is the part most likely to be got wrong. Python's `repr()`
  gives shortest round-tripping digits but switches to exponential at 1e16 rather
  than 1e21, uses exponential for 1e-5 rather than below 1e-6, and zero-pads the
  exponent (`1e-05`). An implementation that reaches for `repr()` will be wrong in
  three distinct ways, all of which look right in casual testing.
- **The committed corpus is a floor, not proof.** Eight vectors cannot cover the
  number space. **Follow-up, owed before M1 closes:** run the upstream
  `cyberphone/json-canonicalization` test data, including the large ES6 number
  file, against the implementation, and record the result. Vendoring that corpus
  needs a license check first, so it is a deliberate step rather than an
  incidental one.
- Rejecting large integers is stricter than RFC 8785 requires and could reject a
  payload another JCS implementation would accept. This is intentional — silent
  precision loss inside an evidence hash is the worse outcome — but it is a real
  interoperability edge, and any imported third-party JSON must be validated
  against it rather than assumed compatible.
- A future non-Python consumer of the evidence format needs its own conforming
  implementation. The corpus is committed partly so that consumer has something to
  test against.

## Rejected alternatives

### A third-party JCS package

Rejected: it adds a pinned runtime dependency to the one package that must install
alone, and it must be audited, license-checked, and tracked for advisories
(constitution §5) to avoid writing roughly a hundred lines of behavior the RFC
fully specifies. The dependency also would not remove the need for the corpus —
the project would still have to prove the library's number formatting is right for
its own hashes.

This rejection is contingent on scope. If canonicalization needs to be shared with
a non-Python component, or the number rule proves harder than the corpus suggests,
a well-maintained library becomes the better trade and warrants a superseding
record.

### `json.dumps(sort_keys=True, separators=(",", ":"))`

Rejected, and worth naming because it is the tempting one-liner. It sorts by code
point rather than UTF-16 code unit, formats numbers by Python's rules rather than
ES6's, escapes non-ASCII by default under `ensure_ascii`, and accepts NaN and
Infinity unless explicitly told not to. It is wrong in four independent ways while
looking exactly like canonical JSON.

### Hashing Python's `pickle` or `repr` of the structure

Rejected: neither is stable across interpreter versions, neither is portable to
another language, and `pickle` is unsafe on untrusted input. An evidence format
that only one interpreter build can verify is not an evidence format.

### Deferring canonicalization until the first immutable record

Rejected: the hash format is the hardest thing to change once records exist,
because changing it invalidates every stored chain. BUILD_ORDER places this
decision before M1 for that reason.

## Notes

The vector corpus is generated rather than hand-written — hand-escaping a
canonical JSON text inside a JSON string is how wrong vectors get committed. Its
correctness rests on the round-trip and ordering assertions in the corpus test,
not on the generator.

Superseding this record would most likely follow from adopting a library (see the
first rejected alternative) or from the upstream corpus revealing a number-format
defect that is cheaper to fix by dependency than by patch.