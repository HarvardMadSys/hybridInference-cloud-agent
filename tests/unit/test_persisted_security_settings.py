"""Saved security settings control runtime behavior without changing the process environment."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cloud_agent import identity, settings, tokens
from cloud_agent.api.deps import get_client_ip
from cloud_agent.api.session import _cookie_is_secure
from cloud_agent.broker_routing import BrokerUrlRefused, validate_broker_url
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.execution_profiles import default_execution_profile
from cloud_agent.source_control import SourceControlCipher


@pytest.fixture(autouse=True)
def saved_settings(monkeypatch):
    settings.reset_settings()
    for definition in settings.SETTINGS:
        if definition.env:
            monkeypatch.delenv(definition.env, raising=False)
    monkeypatch.setenv(
        "AGENT_SETTINGS_ENCRYPTION_KEY", "test-master-key-longer-than-thirty-two-bytes"
    )
    yield
    settings.reset_settings()


def test_sessions_and_worker_tokens_use_saved_keys_and_ignore_stale_environment(monkeypatch):
    encoded = settings.encode_changes(
        {
            "gateway_issuer": "https://identity.example",
            "session_signing_secret": "test-session-key-longer-than-thirty-two-bytes",
            "control_signing_secret": "test-control-signing-key-longer-than-32-bytes",
        }
    )
    settings.apply_settings(1, encoded)
    monkeypatch.setenv("GATEWAY_ISSUER", "https://stale.example")
    monkeypatch.setenv("AGENT_SESSION_SECRET", "stale-session-secret")
    assert identity.gateway_issuer() == "https://identity.example"
    assert identity.verify_session(identity.mint_session(user_id="owner"))["sub"] == "owner"
    assert tokens._signing_key()
    old_key = tokens._signing_key()
    settings.apply_settings(
        2,
        {
            **encoded,
            **settings.encode_changes(
                {"control_signing_secret": "new-control-signing-key-longer-than-32-bytes"}
            ),
        },
    )
    assert tokens._signing_key() != old_key


def test_saved_encryption_keys_work_for_clients_constructed_before_admin_setup():
    compute = ComputeSecretStore(SimpleNamespace())
    assert compute.configured is False
    settings.apply_settings(
        1,
        settings.encode_changes(
            {
                "source_control_encryption_key": "test-user-credential-key",
                "compute_encryption_key": "test-compute-key-longer-than-thirty-two-bytes",
            }
        ),
    )
    assert compute.configured is True
    ciphertext = compute.encrypt("connection", "provider", "private-compute-token")
    assert compute.decrypt("connection", "provider", ciphertext) == "private-compute-token"
    cipher = SourceControlCipher()
    assert cipher.decrypt(cipher.encrypt("private-user-token")) == "private-user-token"


def test_saved_cookie_proxy_broker_and_execution_policies_are_applied():
    settings.apply_settings(
        1,
        settings.encode_changes(
            {
                "session_cookie_secure": False,
                "trust_proxy_headers": True,
                "broker_url_allowed_hosts": "broker.example",
                "default_execution_profile": "untrusted-kata-v1",
            }
        ),
    )
    assert _cookie_is_secure() is False
    assert (
        get_client_ip(SimpleNamespace(headers={"x-forwarded-for": "198.51.100.1"}))
        == "198.51.100.1"
    )
    assert validate_broker_url("https://broker.example") == "https://broker.example"
    with pytest.raises(BrokerUrlRefused):
        validate_broker_url("https://other.example")
    assert default_execution_profile().value == "untrusted-kata-v1"


def test_pgadmin_uses_saved_urls_and_a_saved_reset_ignores_stale_environment(monkeypatch):
    from cloud_agent.api.admin_database import pgadmin_config

    encoded = settings.encode_changes(
        {
            "pgadmin_url": "http://database-tools:5050",
            "web_base_url": "https://agents.example/agents",
        }
    )
    settings.apply_settings(1, encoded)
    monkeypatch.setenv("AGENT_PGADMIN_URL", "http://stale.example")
    monkeypatch.setenv("AGENT_WEB_BASE_URL", "https://stale.example")
    config = pgadmin_config()
    assert config is not None
    assert str(config.upstream) == "http://database-tools:5050"
    assert config.public_url == "https://agents.example/agents/api/admin/agent/pgadmin"
    encoded.update(
        settings.encode_changes(
            {"pgadmin_public_url": "https://proxy.example/separate/admin/agent/pgadmin"}
        )
    )
    settings.apply_settings(2, encoded)
    config = pgadmin_config()
    assert config is not None
    assert config.public_url == "https://proxy.example/separate/admin/agent/pgadmin"
    settings.apply_settings(3, {**encoded, **settings.encode_changes({"pgadmin_url": None})})
    assert pgadmin_config() is None
