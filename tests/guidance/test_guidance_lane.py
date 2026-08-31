"""Guidance lane (spec §12.13).

Guidance is a projection of one server lifecycle state (locked decision 30), so
the names the banner, the WebMCP `next_action`, and the audit trail share must be
registered rather than retyped per surface. The invariant worth locking in before
any copy exists is that the approver role is a human role — locked decision 32
reserves confirmation decisions to a human by design, and guidance copy must not
imply an agent can make one.
"""

import pytest
from actionwitness_core.journeys.enums import EventActor, GuidanceActor


@pytest.mark.guidance
def test_guidance_actors_are_a_closed_set() -> None:
    assert {actor.value for actor in GuidanceActor} == {
        "operator",
        "agent",
        "human_approver",
        "system",
    }


@pytest.mark.guidance
def test_the_approver_role_is_distinct_from_the_agent_role() -> None:
    """An agent can request a protected mutation; it can never authorize one."""
    assert GuidanceActor.HUMAN_APPROVER is not GuidanceActor.AGENT
    assert GuidanceActor.HUMAN_APPROVER.value != GuidanceActor.AGENT.value


@pytest.mark.guidance
def test_event_actors_and_guidance_actors_are_deliberately_different_sets() -> None:
    """Who *did* something and who is *being asked* to act are different questions.

    Conflating them is how a confirmation decision ends up attributed to the
    harness, which would break the audit trail's central claim.
    """
    event_actors = {actor.value for actor in EventActor}
    guidance_actors = {actor.value for actor in GuidanceActor}
    assert event_actors != guidance_actors
    assert "human_approver" not in event_actors
    assert "harness" not in guidance_actors


@pytest.mark.guidance
def test_every_guidance_actor_is_described_for_the_ui() -> None:
    """Guidance copy is derived from the registry, so every role needs its text."""
    from actionwitness_service.api.registry_export import build_registry

    members = build_registry()["enums"]["guidance_actor"]["members"]
    assert set(members) == {actor.value for actor in GuidanceActor}
    assert all(text.strip() for text in members.values())
