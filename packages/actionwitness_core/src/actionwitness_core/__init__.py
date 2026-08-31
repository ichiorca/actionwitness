"""actionwitness_core — target-neutral outcome-assurance library.

Spec: actionwitness-functional-spec.md v1.9, §18 (structure), LD-2/LD-7
(framework-neutral, no app/demo/integration/evaluator-vendor/commerce imports).

Subpackages (all currently scaffolding — no behavior implemented yet):
    contracts   outcome-contract models, parsing, validation (§9.2, §10)
    engine      assertion/policy engine, path resolution, classification (§12.6–12.7, §22)
    evidence    snapshots, events, redaction-facing evidence models, hashing (§9.6, §17.2)
    journeys    run lifecycle and event recording domain logic (§12.4, §16)
    evals       regression eval case factory, fixtures, deterministic replay (§12.9, §24)
    benchmarks  dual-layer correlation protocol, matrix, metrics (§9.9, §12.10, §24.7)
    reports     canonical JSON report models (§23)
    ports       public protocols targets/apps implement (§29.2; see ports/__init__.py)
    security    redaction, limits, canonical hashing helpers (§17.2, §20.3)
"""

__version__ = "0.1.0"
