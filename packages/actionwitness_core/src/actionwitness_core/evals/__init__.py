"""actionwitness_core.evals — regression eval cases and deterministic replay.

Spec v1.9 §9.7–9.8, §12.9, §24.1–24.6.

The core owns the *shape* of a case and the *rule* for matching an expectation.
It does not own what an environment profile means for a given target: §24.4's
`current` → `post_fix` mapping is target knowledge and lives in the integration
layer, because a core that knew `pre_fix` would be a core that knew about one
demo store.
"""

from actionwitness_core.evals.enums import (
    ConfirmationStrategy,
    EvalEnvironment,
    EvalStatus,
    SourceProtocol,
)
from actionwitness_core.evals.models import (
    CASE_SCHEMA_VERSION,
    EmbeddedContract,
    EnvironmentExpectation,
    EvalExpectations,
    EvalFixture,
    EvalReport,
    EvalSource,
    EvalTarget,
    RegressionEvalCase,
    ReplayConfiguration,
    SourceFinding,
    SurfaceDelta,
    SurfaceEvidence,
    TrajectoryStep,
    expectation_matches,
)

__all__ = [
    "CASE_SCHEMA_VERSION",
    "ConfirmationStrategy",
    "EmbeddedContract",
    "EnvironmentExpectation",
    "EvalEnvironment",
    "EvalExpectations",
    "EvalFixture",
    "EvalReport",
    "EvalSource",
    "EvalStatus",
    "EvalTarget",
    "RegressionEvalCase",
    "ReplayConfiguration",
    "SourceFinding",
    "SourceProtocol",
    "SurfaceDelta",
    "SurfaceEvidence",
    "TrajectoryStep",
    "expectation_matches",
]
