"""Plugin request limits and compatibility of existing idempotency keys."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from cloud_agent.domain.requests import request_fingerprint
from cloud_agent.schemas import AgentJobCreate


def test_omitted_plugins_preserve_historical_request_fingerprints():
    original = {"repo": "owner/repo", "task_prompt": "Fix it", "mcp_bindings": None}
    legacy = hashlib.sha256(
        json.dumps({"kind": "create", **original}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert request_fingerprint("create", original) == legacy
    assert request_fingerprint("create", {**original, "plugins": None}) == legacy
    assert request_fingerprint("create", {**original, "plugins": []}) != legacy
    assert (
        request_fingerprint(
            "create",
            {
                **original,
                "plugins": [{"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}],
            },
        )
        != legacy
    )


def test_create_plugins_are_bounded_and_optional():
    body = {"repo": "owner/repo", "task_prompt": "Fix it", "model": "model"}
    assert AgentJobCreate(**body).plugins is None
    selected = {"plugin": "reviewer@team-tools", "marketplace": "owner/plugins"}
    assert len(AgentJobCreate(**body, plugins=[selected] * 8).plugins or []) == 8
    with pytest.raises(ValidationError, match="at most 8"):
        AgentJobCreate(**body, plugins=[selected] * 9)
