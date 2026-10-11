"""Compute integrations and worker cadences adopt saved settings after startup."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from cloud_agent import settings
from cloud_agent.compute_config import (
    ComputeConfiguration,
    current_observation_seconds,
    current_poll_seconds,
    current_registration_url,
)
from cloud_agent.compute_registry import build_registry
from cloud_agent.domain.compute import ComputeRefusal
from cloud_agent.ports.external.compute_provider import (
    UNCHECKED_CLOUDLAB_HOST_KEY_REF,
)
from cloud_agent.providers.compute.cloudlab import CloudLabProvider
from cloud_agent.workers import compute_resources


@pytest.fixture(autouse=True)
def clean_settings():
    settings.reset_settings()
    yield
    settings.reset_settings()


def test_provider_can_be_enabled_changed_and_disabled_without_rebuilding_registry():
    settings.apply_settings(1, {})
    configuration = ComputeConfiguration().with_runtime_settings()
    registry = build_registry(configuration, resolver=AsyncMock(), transport=AsyncMock())
    assert set(registry.entries) == {"existing_ssh"}

    settings.apply_settings(2, {"compute.cloudlab_api_url": "https://portal.example/api"})
    provider = registry.get("cloudlab").provider
    assert set(registry.entries) == {"existing_ssh", "cloudlab"}
    settings.apply_settings(3, {"compute.cloudlab_api_url": "https://new.example/api"})
    assert registry.get("cloudlab").provider is provider
    settings.apply_settings(4, {})
    with pytest.raises(ComputeRefusal):
        registry.get("cloudlab")


def test_cloudlab_ssh_defaults_to_no_host_key_check_without_weakening_existing_ssh():
    configuration = ComputeConfiguration(cloudlab_api_url="https://portal.example/api")
    registry = build_registry(configuration, resolver=AsyncMock(), transport=AsyncMock())
    cloudlab = registry.get("cloudlab")

    unchecked = cloudlab.configuration(
        {"project": "research"}, "cloudlab-account", "management-key"
    )
    assert unchecked["ssh_credential_ref"] == "management-key"
    assert unchecked["host_key_source"] == "known_hosts"
    assert unchecked["ssh_known_hosts_ref"] == UNCHECKED_CLOUDLAB_HOST_KEY_REF

    explicit_unchecked = cloudlab.configuration(
        {"project": "research", "host_key_source": "none"},
        "cloudlab-account",
        "management-key",
    )
    assert explicit_unchecked == unchecked

    verified = cloudlab.configuration(
        {"project": "research", "host_key_source": "known_hosts"},
        "cloudlab-account",
        "management-key",
    )
    assert verified["ssh_known_hosts_ref"] == "management-key:known_hosts"

    registered = cloudlab.configuration(
        {"project": "research", "host_key_source": "registration"},
        "cloudlab-account",
        "management-key",
    )
    assert registered["host_key_source"] == "registration"
    assert "ssh_known_hosts_ref" not in registered

    existing = registry.get("existing_ssh").configuration(
        {"address": "node.example", "username": "agent"},
        "management-key",
        None,
    )
    assert existing["known_hosts_ref"] == "management-key:known_hosts"

    host_key_field = next(
        field for field in cloudlab.catalog()["fields"] if field["key"] == "host_key_source"
    )
    assert host_key_field["default"] == "none"
    assert [option["value"] for option in host_key_field["options"]] == [
        "none",
        "registration",
    ]


def test_key_only_connection_error_does_not_require_a_hidden_host_key_field():
    registry = build_registry(ComputeConfiguration(), resolver=AsyncMock(), transport=AsyncMock())

    with pytest.raises(ComputeRefusal) as refused:
        registry.get("existing_ssh").validate_secret({}, "not-a-private-key")

    assert refused.value.code == "invalid_ssh_key"
    assert "host key" not in str(refused.value).lower()


async def test_endpoint_rotation_affects_next_request_and_preserves_request_in_progress():
    endpoint = "https://first.example/api"
    requests = []

    async def credential(_):
        nonlocal endpoint
        endpoint = "https://second.example/api"
        return "private-token"

    def handler(request):
        requests.append(str(request.url))
        assert request.headers["X-Api-Token"] == "private-token"
        return httpx.Response(200, json={"experiments": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = CloudLabProvider(
            api_url=lambda: endpoint,
            resolver=SimpleNamespace(resolve=credential),
            client=client,
        )
        await provider.validate_connection("connection")
        await provider.validate_connection("connection")
    assert requests == [
        "https://first.example/api/experiments/",
        "https://second.example/api/experiments/",
    ]


def test_explicit_configuration_stays_fixed_while_runtime_reads_current_saved_values():
    fixed = ComputeConfiguration(
        poll_seconds=17,
        observation_seconds=75,
        registration_url="https://fixed.example/v1/compute/node-registrations",
    )
    live = fixed.with_runtime_settings()
    settings.apply_settings(
        1,
        {
            "compute.poll_seconds": 35,
            "compute.observation_seconds": 90,
            "session_redirect_uri": "https://agents.example/api/v1/session/callback",
        },
    )
    assert current_poll_seconds(fixed) == 17
    assert current_observation_seconds(fixed) == 75
    assert current_registration_url(fixed) == fixed.registration_url
    assert current_poll_seconds(live) == 35
    assert current_observation_seconds(live) == 90
    assert (
        current_registration_url(live) == "https://agents.example/api/v1/compute/node-registrations"
    )
    settings.apply_settings(
        2,
        {"compute.registration_url": "https://public.example/v1/compute/node-registrations"},
    )
    assert current_registration_url(live) == "https://public.example/v1/compute/node-registrations"


async def test_scheduler_reloads_poll_interval_between_passes(monkeypatch):
    settings.apply_settings(1, {"compute.poll_seconds": 10})
    configuration = ComputeConfiguration().with_runtime_settings()
    reconciler = SimpleNamespace(
        repository=SimpleNamespace(),
        commands=SimpleNamespace(configuration=configuration),
        schedule=AsyncMock(),
    )
    worker = compute_resources.ComputeResourceWorker(reconciler)
    delays = []

    async def tick(delay):
        delays.append(delay)
        settings.apply_settings(2, {"compute.poll_seconds": 25})
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(compute_resources, "asyncio", SimpleNamespace(sleep=tick))
    with pytest.raises(asyncio.CancelledError):
        await worker._schedule()
    assert delays == [10, 25]
