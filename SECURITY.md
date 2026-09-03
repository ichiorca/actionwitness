# Security policy

## Supported scope

Security fixes target the latest `main` branch and the current public demo. This
hackathon build is an MVP, not a hosted multi-tenant or production-commerce service.

## Reporting a vulnerability

Do not include credentials, personal data, exploit payloads, or sensitive target
details in a public issue.

Use GitHub's private **Report a vulnerability** flow from the repository Security
tab when it is available. If private reporting is not enabled, open a minimal issue
requesting a private contact channel and include only:

- the affected component or route;
- the broad impact category;
- whether active exploitation is suspected;
- a safe way to contact you.

The maintainer will acknowledge the report, establish a private channel, assess
scope, and coordinate remediation and disclosure. Exposed secrets require immediate
containment and rotation before ordinary development resumes.

## Threat boundary

ActionWitness treats HTTP bodies, WebMCP arguments and results, imported reports,
persisted records, URLs, browser storage, messages, and adapter responses as
untrusted input.

Core security properties include:

- explicit Pydantic validation at Python service boundaries;
- runtime narrowing of external `unknown` values in TypeScript;
- workspace-scoped authorization through a cryptographically random, secure cookie;
- origin validation on mutations and no cross-origin API surface;
- a strict Content Security Policy and same-origin tools policy;
- server-issued, expiring human confirmation bound to exact mutation intent;
- stable idempotency keys and fail-closed conflicting reuse;
- operator-configured adapter origins with redirect and final-URL validation;
- append-only, canonical, hash-linked evidence with verification before trust;
- structured logs that exclude payloads, credentials, and personal data;
- secret-shape and release-artifact hygiene checks in CI.

A tool's self-report is evidence, never proof. Observation failure, ambiguous
source, or evidence-integrity failure produces an explicit non-pass result.

## Out of scope

Do not test ActionWitness against production stores, payment systems, checkout,
orders, arbitrary remote targets, bulk data, or any origin you do not own or have
explicit authorization to assess.

The public Buggy Store is deliberately failure-injectable and contains no real
commerce, credential, customer, or payment data.
