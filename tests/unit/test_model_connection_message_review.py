"""A malformed queued-message edit cannot silently discard a personal source."""

import pytest
from pydantic import ValidationError

from cloud_agent.schemas.messages import AgentPendingMessageUpdate


@pytest.mark.parametrize("model", [None, ""])
def test_a_personal_source_requires_an_actual_model_in_a_queue_edit(model):
    with pytest.raises(ValidationError, match="model_connection_id needs model"):
        AgentPendingMessageUpdate(revision=1, model=model, model_connection_id="mconn_" + "a" * 32)


def test_an_explicit_null_pair_can_restore_inheritance():
    update = AgentPendingMessageUpdate(revision=1, model=None, model_connection_id=None)
    assert update.model is None and update.model_connection_id is None
