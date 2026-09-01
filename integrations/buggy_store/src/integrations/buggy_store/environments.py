"""What §24.4's environment profiles mean for the Buggy Store (007-T8).

§24.4 names two profiles in target-neutral words:

- `current` — "request the target adapter's corrected/current implementation.
  **For Buggy Store this maps to `post_fix` with no active injected failure**
  and is the CI default."
- `reproduce_source` — "request the immutable source scenario. For a Buggy Store
  failure case this maps to `pre_fix` plus the source run's known failure
  profile."

**This mapping lives here and not in the core.** `pre_fix` and `post_fix` are
this store's vocabulary; a core that knew them would be a core that knew about
one demo application, and the next target would have to pretend to have modes it
does not. The core names the profiles and compares outcomes; the integration
says what each profile *is*.

The asymmetry is deliberate and worth stating: `current` deliberately discards
the case's recorded failure profile, while `reproduce_source` deliberately
restores it. A `current` run that inherited the fault would fail forever and CI
would learn nothing; a `reproduce_source` run that dropped it could not
reproduce the failure it exists to reproduce.
"""

from __future__ import annotations

from actionwitness_core.evals.enums import EvalEnvironment
from actionwitness_core.ports.models import ScenarioSelection

__all__ = ["CURRENT_MODE", "SOURCE_MODE", "scenario_for"]

#: The corrected implementation. §24.4: "`post_fix` with no active injected
#: failure".
CURRENT_MODE = "post_fix"
#: The mode a Buggy Store failure case was recorded under.
SOURCE_MODE = "pre_fix"


def scenario_for(
    environment: EvalEnvironment,
    *,
    source_scenario_mode: str | None,
    source_failure_profile: str | None,
) -> ScenarioSelection:
    """The scenario a replay should restore for one profile.

    `current` ignores both recorded values on purpose. §24.2 step 9 keeps the
    source failure profile as *provenance*, and honouring it here would turn
    provenance into configuration — every `current` run would reproduce the bug
    and no CI job could ever go green.

    `reproduce_source` uses the recorded mode when the case carries one, and
    falls back to `pre_fix` when it does not: a case cut from a failure whose
    mode was never recorded is still a case about the source implementation, and
    guessing `post_fix` would silently run the corrected code under the label
    "reproduce".
    """
    if environment is EvalEnvironment.CURRENT:
        return ScenarioSelection(scenario_mode=CURRENT_MODE, fault_profile=None)

    return ScenarioSelection(
        scenario_mode=source_scenario_mode or SOURCE_MODE,
        fault_profile=source_failure_profile,
    )
