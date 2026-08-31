"""Registry of ManagedTargetAdapter/ExternalTargetAdapter/ObservationProvider
implementations (actionwitness_core.ports). Composition root registers integrations here;
a missing adapter yields a clear unavailable state, not a process failure (§26.7).
Scaffolding only.
"""
