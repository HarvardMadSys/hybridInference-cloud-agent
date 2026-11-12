"""Models a deployment provides: allowances, the relay's route list, the owner tag."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from cloud_agent.domain.model_connections import ModelConnectionInvalid
from cloud_agent.domain.operator_models import (
    Allowance,
    clean_default_model,
    clean_end_user_header,
    effective_allowance,
    end_user_tag,
    next_reset,
    relay_route,
)


@pytest.mark.parametrize(
    ("default", "own", "limit"),
    [
        # No default set: offered to nobody, whatever an account's own row says.
        (None, None, 0),
        (None, {"daily_requests": 50}, 0),
        ({"user_daily_requests": 100}, None, 100),
        ({"user_daily_requests": None}, None, None),
        # An account's own number replaces the default, in both directions.
        ({"user_daily_requests": 0}, {"daily_requests": 25}, 25),
        ({"user_daily_requests": 100}, {"daily_requests": 0}, 0),
        ({"user_daily_requests": 100}, {"daily_requests": None}, None),
    ],
)
def test_an_accounts_own_allowance_replaces_the_models_default(default, own, limit) -> None:
    assert effective_allowance(default, own).limit == limit


def test_zero_withholds_a_model_and_none_never_runs_out() -> None:
    assert not Allowance(limit=0).offered
    assert Allowance(limit=None, used=10**9).offered
    assert not Allowance(limit=None, used=10**9).exhausted
    assert not Allowance(limit=3, used=2).exhausted
    assert Allowance(limit=3, used=3).exhausted


def test_counts_start_over_at_the_next_utc_midnight() -> None:
    late = datetime(2026, 11, 12, 23, 59, 59, tzinfo=UTC)
    assert next_reset(late) == datetime(2026, 11, 13, tzinfo=UTC)
    assert next_reset(datetime(2026, 11, 13, tzinfo=UTC)) == datetime(2026, 11, 14, tzinfo=UTC)


@pytest.mark.parametrize(
    ("base_url", "method", "path", "inference", "anthropic"),
    [
        ("https://p.example/v1", "POST", "/v1/chat/completions", True, False),
        ("https://p.example/v1", "POST", "/v1/responses", True, False),
        ("https://p.example/v1", "GET", "/v1/models", False, False),
        # One trailing /v1 is dropped for Anthropic Messages, as the CLI's SDK appends its own.
        ("https://p.example/v1", "POST", "/v1/messages", True, True),
        ("https://p.example/v1", "POST", "/v1/messages/count_tokens", False, True),
        ("https://p.example/api/v1", "POST", "/api/v1/chat/completions", True, False),
        ("https://p.example/api/v1", "POST", "/api/v1/messages", True, True),
        ("https://p.example/anthropic", "POST", "/anthropic/v1/messages", True, True),
        ("https://p.example/anthropic", "GET", "/anthropic/v1/models", False, True),
        ("https://p.example", "POST", "/chat/completions", True, False),
        ("https://p.example", "POST", "/v1/messages", True, True),
    ],
)
def test_the_relay_forwards_the_paths_an_agent_cli_calls(
    base_url, method, path, inference, anthropic
) -> None:
    route = relay_route(base_url, method, path)
    assert route is not None
    assert (route.inference, route.anthropic) == (inference, anthropic)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        # Whatever else the provider serves under the same prefix.
        ("GET", "/v1/responses/resp_123"),
        ("POST", "/v1/responses/resp_123/cancel"),
        ("DELETE", "/v1/responses"),
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/files"),
        ("GET", "/v1/chat/completions"),
        ("POST", "/v1/models"),
        # Another spelling of a listed path is another path here.
        ("POST", "/v1/chat/completions/"),
        ("POST", "/v1/./chat/completions"),
        ("POST", "/v1/x/../chat/completions"),
        ("POST", "/v1/chat%2Fcompletions"),
        ("POST", "/v1/chat/completion%73"),
        ("POST", "//v1/chat/completions"),
        ("POST", "/V1/chat/completions"),
        # A path of another connection's shape.
        ("POST", "/chat/completions"),
        ("POST", "/api/v1/chat/completions"),
        ("POST", "/"),
        ("POST", ""),
    ],
)
def test_the_relay_forwards_nothing_else(method, path) -> None:
    assert relay_route("https://p.example/v1", method, path) is None


def test_an_owner_tag_is_stable_opaque_and_keyed() -> None:
    tag = end_user_tag("root-key", "01HZXACCOUNT")
    assert tag == end_user_tag("root-key", "01HZXACCOUNT")
    assert len(tag) == 24 and all(ch in "0123456789abcdef" for ch in tag)
    assert "01HZX" not in tag.upper()
    assert tag != end_user_tag("root-key", "01HZXOTHER")
    # Not computable without the deployment's key.
    assert tag != end_user_tag("another-key", "01HZXACCOUNT")
    with pytest.raises(ValueError, match="needs"):
        end_user_tag("", "01HZXACCOUNT")


@pytest.mark.parametrize("name", ["X-End-User", "x-user-id", "OpenAI-User"])
def test_an_owner_tag_header_is_a_plain_header_name(name) -> None:
    assert clean_end_user_header(f"  {name} ") == name


@pytest.mark.parametrize("empty", [None, "", "   "])
def test_no_owner_tag_header_sends_none(empty) -> None:
    assert clean_end_user_header(empty) is None


@pytest.mark.parametrize(
    "name",
    ["Authorization", "x-api-key", "Host", "Content-Length", "X End", "X-End:User", "é", "1x"],
)
def test_an_owner_tag_header_cannot_be_one_the_relay_sets(name) -> None:
    with pytest.raises(ModelConnectionInvalid):
        clean_end_user_header(name)


def test_the_default_model_is_one_of_the_listed_models() -> None:
    assert clean_default_model(" m1 ", ["m1", "m2"]) == "m1"
    assert clean_default_model("", ["m1"]) is None
    assert clean_default_model(None, ["m1"]) is None
    with pytest.raises(ModelConnectionInvalid):
        clean_default_model("m3", ["m1", "m2"])
