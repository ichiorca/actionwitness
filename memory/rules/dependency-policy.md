---
title: Dependency policy
scope: project
---

- Prefer the standard library and already-present dependencies before adding a new one.
- Justify each new dependency: what it does that existing code can't, and its maintenance/security cost.
- Pin versions; avoid floating ranges. Review lockfile changes; regenerate via the package manager, never hand-edit.
- Vet new dependencies: maintenance status, transitive footprint, license, known advisories.
- Prefer well-maintained, widely-used libraries over abandoned or single-maintainer ones for critical paths.
- Don't vendor or copy-paste library code to dodge the dependency process — surface the tradeoff instead.
