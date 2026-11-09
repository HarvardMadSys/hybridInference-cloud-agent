"""Shared fixtures for the server tests."""

from __future__ import annotations

from typing import Any

import pytest

from cloud_agent.api import model_connections as api
from cloud_agent.application import model_connection_save as save
from cloud_agent_shared.model_connections import PROTOCOLS


class _Listings:
    """Stands in for ``read_effort_facts``: records each read, answers per model.

    Unless a test says otherwise, the provider's listings declare nothing.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.answers: dict[str, dict[str, dict[str, Any]]] = {}
        self.during: Any = None

    async def __call__(self, **kwargs: Any) -> dict[str, dict[str, Any]]:
        self.calls.append({k: kwargs[k] for k in ("base_url", "api_key", "models")})
        if self.during is not None:
            await self.during(kwargs)
        undeclared = {
            "status": "unknown",
            "levels": [],
            "source": "",
            "reason": "listing_unavailable",
        }
        return {
            model: {
                "efforts": self.answers.get(model, dict.fromkeys(PROTOCOLS, undeclared)),
                "efforts_checked_at": "2026-11-05T00:00:00+00:00",
            }
            for model in kwargs["models"]
        }


@pytest.fixture(autouse=True)
def listings(monkeypatch) -> _Listings:
    """No server test reads a real provider's listing; each read is recorded."""
    fake = _Listings()
    monkeypatch.setattr(save, "read_effort_facts", fake)
    monkeypatch.setattr(api, "read_effort_facts", fake, raising=False)
    return fake
