"""A provided connection's claim: relayed, with no key in it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cloud_agent_shared.model_connections import (
    CONNECTION_ID,
    ModelConnectionAccess,
    is_operator_connection_id,
    relayed_base_url,
)

_OWNER = "mconn_" + "1" * 32
_PROVIDED = "oconn_" + "2" * 32


def _access(**fields):
    base = {
        "connection_id": _PROVIDED,
        "revision": 4,
        "base_url": "https://provider.example/v1",
        "protocol": "chat_completions",
        "relayed": True,
    }
    return ModelConnectionAccess(**{**base, **fields})


def test_both_kinds_of_id_name_a_turns_source() -> None:
    assert CONNECTION_ID.fullmatch(_OWNER) and CONNECTION_ID.fullmatch(_PROVIDED)
    assert is_operator_connection_id(_PROVIDED)
    assert not is_operator_connection_id(_OWNER)
    assert not is_operator_connection_id("oconn_short")
    assert not is_operator_connection_id(None)


def test_a_relayed_claim_carries_no_key() -> None:
    access = _access()
    sent = access.model_dump(mode="json")
    assert sent == {
        "connection_id": _PROVIDED,
        "revision": 4,
        "base_url": "https://provider.example/v1",
        "protocol": "chat_completions",
        "relayed": True,
    }
    assert access.secrets() == {}
    assert access.launch_fields() == {
        "inference_base_url": "https://provider.example/v1",
        "inference_protocol": "chat_completions",
        "inference_source": f"{_PROVIDED}@4",
        "inference_relayed": "1",
    }
    # What a host reads back is what was sent.
    assert ModelConnectionAccess.model_validate(sent) == access


def test_an_owner_claim_is_what_it_was() -> None:
    access = ModelConnectionAccess(
        connection_id=_OWNER,
        revision=1,
        base_url="https://provider.example/v1",
        protocol="responses",
        api_key="sk-owner-key",
    )
    sent = access.model_dump(mode="json")
    assert "relayed" not in sent and sent["api_key"] == "sk-owner-key"
    assert "inference_relayed" not in access.launch_fields()
    assert access.secrets() == {f"model-connection:{_OWNER}": "sk-owner-key"}


def test_a_residencys_placeholder_is_masked_like_a_key() -> None:
    access = _access(api_key="amd.payload.signature")
    assert access.secrets() == {f"model-connection:{_PROVIDED}": "amd.payload.signature"}
    assert "amd.payload" not in repr(access)


@pytest.mark.parametrize(
    "fields",
    [
        # An owner's connection is never relayed, and always carries its key.
        {"connection_id": _OWNER, "relayed": True},
        {"connection_id": _OWNER, "relayed": False},
        {"connection_id": _OWNER, "relayed": False, "api_key": ""},
        # A provided one is always relayed.
        {"connection_id": _PROVIDED, "relayed": False, "api_key": "sk-deployment-key"},
        {"connection_id": "xconn_" + "0" * 32},
    ],
)
def test_the_two_kinds_cannot_be_mixed(fields) -> None:
    with pytest.raises(ValidationError):
        _access(**fields)


def test_a_translated_relayed_claim_still_carries_its_members_token() -> None:
    access = _access(client_protocol="responses", local_token="a" * 64)
    assert access.launch_fields()["inference_local_token"] == "a" * 64
    assert access.launch_fields()["inference_relayed"] == "1"
    assert access.secrets() == {f"model-connection-translator:{_PROVIDED}": "a" * 64}


@pytest.mark.parametrize(
    ("base_url", "expected"),
    [
        ("https://provider.example/v1", "http://agent-models:8000/v1"),
        ("https://provider.example", "http://agent-models:8000"),
        ("https://provider.example/api/v1", "http://agent-models:8000/api/v1"),
        ("https://provider.example/anthropic", "http://agent-models:8000/anthropic"),
    ],
)
def test_a_relayed_member_is_launched_at_the_connections_path_on_the_relay(
    base_url, expected
) -> None:
    assert relayed_base_url(base_url, "http://agent-models:8000") == expected
    assert relayed_base_url(base_url, "http://agent-models:8000/") == expected
