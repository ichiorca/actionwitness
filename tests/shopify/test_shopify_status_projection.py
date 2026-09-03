"""Operator-facing Shopify status stays honest when stored evidence is corrupt."""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = [pytest.mark.shopify, pytest.mark.integration]


async def test_status_refuses_a_tampered_cart_snapshot(app: Any, trial: Any) -> None:
    """A proof panel must never render altered state as authoritative evidence."""
    # Arrange - complete the public pairing flow through its initial observation,
    # then model out-of-band storage tampering without recomputing the hash.
    pairing_id, _session = await trial.armed()
    run_id = (await trial.status(pairing_id)).json()["pairing"]["run_id"]
    async with app.state.database.transaction() as work:
        await work.execute(
            "UPDATE snapshots SET redacted_state_json = ? WHERE run_id = ? AND phase = ?",
            ('{"cart":{"item_count":999}}', run_id, "before"),
        )

    # Act
    response = await trial.status(pairing_id)

    # Assert - bounded failure, no partial observation and no altered value.
    assert response.status_code == 500, response.text
    assert response.json()["error"] == {
        "code": "HARNESS_ERROR",
        "message": "Stored Shopify cart evidence failed its integrity check and was not served.",
        "details": [],
        "retryable": False,
    }
    assert "observations" not in response.text
    assert "999" not in response.text
