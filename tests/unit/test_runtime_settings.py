"""What a host applies for each runtime setting, and what it reports applying."""

from __future__ import annotations

import json

import httpx
import pytest

from cloud_agent_host.runtime_settings import RuntimeSettings
from cloud_agent_host.session_supervisor_client import (
    SessionSupervisorClient,
    SessionSupervisorError,
)
from cloud_agent_shared.host_settings import SANDBOX_MANAGER_SETTING_KEYS, default_values

LOCAL = {
    "agent_timeout_s": 3600.0,
    "chat_idle_timeout_s": 1800.0,
    "source_import_max_mib": 1024,
    "shared_resident_capacity": 8,
}


class _SandboxManager:
    """The sandbox manager's side of the push: resolve its share over its own values."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[dict[str, int]] = []
        self.local = {
            "terminal_idle_ttl_s": 1800,
            "terminal_hard_ttl_s": 28800,
            "terminal_max_sessions": 64,
            "chat_idle_timeout_s": 1800,
        }

    def __call__(self, settings: dict[str, int]) -> dict:
        self.sent.append(settings)
        if self.fail:
            raise ConnectionError("sandbox manager down")
        return {"applied": {**self.local, **settings}, "local": self.local}


def test_with_no_admin_values_every_setting_is_the_hosts_own():
    settings = RuntimeSettings(LOCAL)

    assert settings.turn_settings() == LOCAL
    applied, local = settings.report()
    assert (
        applied
        == local
        == {
            "agent_timeout_s": 3600,
            "chat_idle_timeout_s": 1800,
            "source_import_max_mib": 1024,
            "shared_resident_capacity": 8,
        }
    )


def test_admin_values_win_for_the_next_turn_and_clearing_one_returns_the_hosts_own():
    settings = RuntimeSettings(LOCAL)

    assert settings.apply({"agent_timeout_s": 900, "source_import_max_mib": 4096}) is True
    assert settings.turn_settings() == {
        **LOCAL,
        "agent_timeout_s": 900,
        "source_import_max_mib": 4096,
    }

    settings.apply({"agent_timeout_s": 900})
    assert settings.turn_settings()["source_import_max_mib"] == 1024


def test_the_sandbox_manager_is_sent_only_its_share_and_its_answer_is_what_is_reported():
    sandbox_manager = _SandboxManager()
    settings = RuntimeSettings(LOCAL, sandbox_manager=sandbox_manager)

    settings.apply(
        {"agent_timeout_s": 900, "terminal_max_sessions": 16, "chat_idle_timeout_s": 600}
    )

    assert sandbox_manager.sent == [
        {
            **default_values(SANDBOX_MANAGER_SETTING_KEYS),
            "terminal_max_sessions": 16,
            "chat_idle_timeout_s": 600,
        }
    ]
    applied, local = settings.report()
    assert applied == {
        **default_values(SANDBOX_MANAGER_SETTING_KEYS),
        "agent_timeout_s": 900,
        "chat_idle_timeout_s": 600,
        "source_import_max_mib": 1024,
        "shared_resident_capacity": 8,
        "terminal_idle_ttl_s": 1800,
        "terminal_hard_ttl_s": 28800,
        "terminal_max_sessions": 16,
    }
    assert local["terminal_max_sessions"] == 64
    assert local["agent_timeout_s"] == 3600


def test_an_unreachable_sandbox_manager_leaves_its_settings_unconfirmed_not_assumed():
    """The page must see the sandbox manager's settings as not applied, never guess they were."""
    sandbox_manager = _SandboxManager(fail=True)
    settings = RuntimeSettings(LOCAL, sandbox_manager=sandbox_manager)

    settings.apply({"terminal_max_sessions": 16})

    applied, local = settings.report()
    assert "terminal_max_sessions" not in applied
    assert "terminal_max_sessions" not in local
    # The runner's own share is unaffected by the sandbox manager being down.
    assert applied["agent_timeout_s"] == 3600


def test_apply_reports_a_change_only_when_the_report_would_change():
    """A changed report is sent at once; an unchanged one waits for the interval."""
    settings = RuntimeSettings(LOCAL, sandbox_manager=_SandboxManager())

    assert settings.apply({}) is True  # the sandbox manager's first confirmation
    assert settings.apply({}) is False
    assert settings.apply({"agent_timeout_s": 900}) is True
    assert settings.apply({"agent_timeout_s": 900}) is False


def test_adopting_defaults_without_a_sandbox_manager_does_not_resend_an_unchanged_report():
    settings = RuntimeSettings(LOCAL)
    previous = settings.report()
    assert settings.apply({}) is False
    assert settings.adopted
    assert settings.report() == previous


@pytest.mark.parametrize(
    "admin",
    [
        {"agent_timeout_s": 10},
        {"agent_timeout_s": True},
        {"agent_timeout_s": "900"},
        {"from_a_newer_release": 3},
    ],
)
def test_a_value_this_host_cannot_apply_is_ignored(admin):
    settings = RuntimeSettings(LOCAL)

    settings.apply(admin)

    assert settings.turn_settings() == LOCAL


def test_the_runner_never_holds_a_setting_only_the_sandbox_manager_applies():
    settings = RuntimeSettings({**LOCAL, "terminal_max_sessions": 64})

    settings.apply({"terminal_max_sessions": 16})

    assert "terminal_max_sessions" not in settings.turn_settings()


def _client(handler) -> SessionSupervisorClient:
    client = SessionSupervisorClient("http://sandbox_manager", "sandbox-manager-secret")
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"X-Agent-Workspace-Token": "sandbox-manager-secret"},
    )
    return client


def test_the_sandbox_manager_client_sends_the_settings_and_returns_the_sandbox_managers_answer():
    seen: list[tuple[str, dict, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.path,
                json.loads(request.content),
                request.headers["X-Agent-Workspace-Token"],
            )
        )
        return httpx.Response(200, json={"applied": {"terminal_max_sessions": 16}, "local": {}})

    client = _client(handler)
    try:
        answer = client.apply_host_settings({"terminal_max_sessions": 16})
    finally:
        client.close()

    assert seen == [
        ("/host-settings", {"settings": {"terminal_max_sessions": 16}}, "sandbox-manager-secret")
    ]
    assert answer == {"applied": {"terminal_max_sessions": 16}, "local": {}}


@pytest.mark.parametrize("status", [401, 422, 503])
def test_a_sandbox_manager_refusal_is_an_error_not_a_confirmation(status):
    client = _client(lambda _request: httpx.Response(status, json={"detail": "no"}))
    try:
        with pytest.raises(SessionSupervisorError) as raised:
            client.apply_host_settings({})
    finally:
        client.close()

    assert raised.value.status_code == status


def test_the_runners_share_is_in_force_before_the_sandbox_manager_is_asked():
    """A slow sandbox manager must not hold up the values the next turn is handed."""
    seen_during_push: list[dict[str, float]] = []
    settings: RuntimeSettings

    def slow_sandbox_manager(_settings: dict[str, int]) -> dict:
        seen_during_push.append(settings.turn_settings())
        assert settings.adopted
        raise TimeoutError("sandbox manager did not answer")

    settings = RuntimeSettings(LOCAL, sandbox_manager=slow_sandbox_manager)
    assert not settings.adopted

    settings.apply({"agent_timeout_s": 900})

    assert seen_during_push == [{**LOCAL, "agent_timeout_s": 900}]


@pytest.mark.parametrize(
    ("sandbox_manager", "reported"),
    [
        # The sandbox manager confirmed the same value: in force in both processes.
        (_SandboxManager(), 600),
        # The sandbox manager did not answer: not reported, so the page keeps saying applying.
        (_SandboxManager(fail=True), None),
    ],
)
def test_a_setting_both_processes_apply_is_reported_only_when_both_agree(sandbox_manager, reported):
    settings = RuntimeSettings(LOCAL, sandbox_manager=sandbox_manager)

    settings.apply({"chat_idle_timeout_s": 600})

    applied, _local = settings.report()
    assert applied.get("chat_idle_timeout_s") == reported
    assert settings.turn_settings()["chat_idle_timeout_s"] == 600, (
        "the runner applies it either way"
    )


def test_a_sandbox_manager_that_applies_another_value_is_reported_at_its_own():
    """A sandbox manager on an older release can drop a value the runner accepted."""

    def stubborn(_settings: dict[str, int]) -> dict:
        return {"applied": {"chat_idle_timeout_s": 1800}, "local": {"chat_idle_timeout_s": 1800}}

    settings = RuntimeSettings(LOCAL, sandbox_manager=stubborn)
    settings.apply({"chat_idle_timeout_s": 600})

    applied, _local = settings.report()
    assert applied["chat_idle_timeout_s"] == 1800


def test_with_no_sandbox_manager_the_runners_value_is_the_whole_truth():
    settings = RuntimeSettings(LOCAL)

    settings.apply({"chat_idle_timeout_s": 600})

    applied, _local = settings.report()
    assert applied["chat_idle_timeout_s"] == 600


def test_first_adoption_and_reset_replace_legacy_environment_values():
    legacy = {**LOCAL, "agent_timeout_s": 7200, "chat_idle_timeout_s": 600}
    sandbox_manager = _SandboxManager()
    sandbox_manager.local["terminal_max_sessions"] = 99
    settings = RuntimeSettings(legacy, sandbox_manager=sandbox_manager)
    assert settings.turn_settings()["agent_timeout_s"] == 7200

    settings.apply({})
    assert settings.turn_settings() == LOCAL
    applied, local = settings.report()
    assert applied["terminal_max_sessions"] == 64
    assert local["terminal_max_sessions"] == 99
    assert local["agent_timeout_s"] == 7200

    settings.apply({"agent_timeout_s": 900, "terminal_max_sessions": 16})
    settings.apply({})
    assert settings.turn_settings() == LOCAL
    assert settings.report()[0]["terminal_max_sessions"] == 64
