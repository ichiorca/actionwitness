---
title: Security baseline
scope: project
---

- NEVER hardcode secrets (API keys, tokens, passwords). Use env vars or a secret manager; read them at startup.
- Validate and sanitize ALL external input. Use parameterized queries (no string-built SQL); escape output to prevent XSS.
- Enforce authentication and authorization on every protected operation — verify, don't assume.
- Apply least privilege: the narrowest scope/permission/token that works.
- Never log secrets, credentials, or regulated/PII data. Keep error messages free of sensitive detail.
- If a secret is exposed, STOP, rotate it, and scrub it from history before continuing.
- Pin and audit dependencies; treat a new dependency as new attack surface.
