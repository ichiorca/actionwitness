"""Dual-layer benchmark vocabulary and arithmetic (§9.9, §12.10, §16.4).

The core owns the *shape* of a benchmark and the *arithmetic* over it. It does
not own what a `webmcp-evals` report looks like — that is one experimental
upstream package's JSON, and it lives in `integrations.google_evals` behind the
adapter boundary. A core that knew the reporter's field names would be a core
that had to change when upstream renamed one.

**The two layers never merge.** §9.9: the benchmark "measures correlation and
incremental defect detection, not which evaluator is universally better". Every
model here keeps the call-level result and the outcome result in separate
fields, and FR-092 forbids any metric that implies one layer replaces or shares
ground truth with the other. The cell where they disagree — call-level pass,
outcome fail — is the product's whole thesis, and collapsing them into a single
score would delete it.
"""

from __future__ import annotations

__all__: list[str] = []
