"""An Idempotency-Key fingerprint survives a deploy that adds a request field."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from cloud_agent.domain import requests
from cloud_agent.domain.requests import request_fingerprint
from cloud_agent.schemas import AgentJobCreate, AgentThreadMessageCreate

_CREATE = {"repo": "owner/repo", "task_prompt": "Fix it", "model": "m"}


def _previous_rule(kind: str, payload: dict[str, Any]) -> str:
    """The rule the fingerprints already stored against live keys were made with.

    The last one released, which had learned to drop a null ``plugins`` and an
    unset repository environment too; the releases before it never sent those
    fields, so they all agree.
    """
    kept = {
        key: value
        for key, value in payload.items()
        if (key not in {"skill_ids", "plugins", "expected_latest_job_id"} or value is not None)
        and (key != "require_idle" or value is not False)
        and not (
            key in {"environment_id", "environment_revision", "global_revision"} and value is None
        )
        and not (key == "environment_overrides" and not value)
        # Added after every stored fingerprint: no earlier send carried it.
        and not (key == "ssh_server_ids" and value is None)
    }
    canonical = json.dumps(
        {"kind": kind, **kept}, sort_keys=True, separators=(",", ":"), default=str
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _message(**fields: Any) -> dict[str, Any]:
    return {"thread_id": "t-1", **AgentThreadMessageCreate(**fields).model_dump(mode="json")}


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        ("create", AgentJobCreate(**_CREATE).model_dump(mode="json")),
        ("create", AgentJobCreate(**_CREATE, skill_ids=[1], metadata={"k": "v"}).model_dump()),
        ("thread_message", _message(prompt="and then")),
        (
            "thread_message",
            _message(prompt="watch it", require_idle=True, expected_latest_job_id="job-1"),
        ),
    ],
)
def test_fingerprints_already_stored_keep_their_exact_bytes(
    kind: str, payload: dict[str, Any]
) -> None:
    """A resend after the upgrade must still match the send made before it."""
    assert request_fingerprint(kind, payload) == _previous_rule(kind, payload)


def test_a_field_added_to_a_create_later_is_left_out_while_unset() -> None:
    """``model_dump`` names every declared field, so a new one arrives as null."""

    class LaterCreate(AgentJobCreate):
        later_option: str | None = None

    before = request_fingerprint("create", AgentJobCreate(**_CREATE).model_dump(mode="json"))
    after = LaterCreate(**_CREATE)
    assert request_fingerprint("create", after.model_dump(mode="json")) == before
    # Set, it is part of what was asked for, so the same key cannot replay it.
    chosen = LaterCreate(**_CREATE, later_option="x").model_dump(mode="json")
    assert request_fingerprint("create", chosen) != before


def test_a_field_added_to_a_thread_message_later_is_left_out_while_unset() -> None:
    class LaterMessage(AgentThreadMessageCreate):
        later_option: int | None = None

    before = request_fingerprint("thread_message", _message(prompt="and then"))
    after = {"thread_id": "t-1", **LaterMessage(prompt="and then").model_dump(mode="json")}
    assert request_fingerprint("thread_message", after) == before


def test_a_historical_null_is_still_part_of_the_hash() -> None:
    """Dropping one would change every fingerprint stored with it."""
    with_null = {**_CREATE, "metadata": None}
    assert request_fingerprint("create", with_null) != request_fingerprint("create", _CREATE)


@pytest.mark.parametrize("model", [AgentJobCreate, AgentThreadMessageCreate])
def test_every_later_request_field_is_omitted_by_a_value_the_rule_drops(model: Any) -> None:
    """A later field whose omission sends anything else would move every hash.

    This is what keeps the frozen set honest: a new field defaulting to null
    needs nothing, one defaulting to ``False`` must be named beside
    ``require_idle``, and any other default needs its own decision here.
    """
    for name, field in model.model_fields.items():
        if name in requests._HISTORICAL_FIELDS:
            continue
        assert field.default is None or (
            name in requests._FALSE_WHEN_OMITTED and field.default is False
        ), name
