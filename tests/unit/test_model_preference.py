"""A new task starts where the owner's newest turn ran, while that connection can serve it."""

from __future__ import annotations

import pytest

from cloud_agent.model_preference import ModelPreferenceView, resolve_connection_default

_CONNECTION = {"id": "mconn_1", "enabled": True, "models": ["model-a", "model-b"]}


def _last(model: str | None = "model-a", connection: str | None = "mconn_1"):
    return ModelPreferenceView(last_model=model, last_model_connection_id=connection)


def test_a_new_task_starts_on_the_last_model_and_connection():
    assert resolve_connection_default(_last(), [_CONNECTION]) == ("model-a", "mconn_1")


@pytest.mark.parametrize(
    ("preference", "connections"),
    [
        (_last(model=None), [_CONNECTION]),
        (_last(connection=None), [_CONNECTION]),
        (_last(model="removed-model"), [_CONNECTION]),
        (_last(connection="mconn_deleted"), [_CONNECTION]),
        (_last(), [{**_CONNECTION, "enabled": False}]),
        (_last(), [{**_CONNECTION, "models": None}]),
        (_last(), []),
    ],
    ids=[
        "never-used",
        "turn-before-connections",
        "model-no-longer-listed",
        "connection-deleted",
        "connection-disabled",
        "no-model-list",
        "no-connections",
    ],
)
def test_nothing_is_preselected_when_that_connection_cannot_serve_it(preference, connections):
    assert resolve_connection_default(preference, connections) is None
