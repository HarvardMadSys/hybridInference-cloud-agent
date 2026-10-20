"""Kilo: the pinned CLI's ``serve`` behind the HTTP bridge."""

from __future__ import annotations

from cloud_agent_shared.runtime_catalog import KILO

from .http_bridge import HttpBridgeRuntime


class KiloRuntime(HttpBridgeRuntime):
    """Kilo Code, through the ``kilo-freeinference`` wrapper.

    Kilo's CLI is an OpenCode fork and keeps both of OpenCode's gaps:
    ``OPENAI_BASE_URL`` is ignored, and startup fetches the models.dev
    catalog. The ``kilo-freeinference`` wrapper closes them the same way the
    OpenCode wrapper does — the fork renamed every kill switch to ``KILO_*``
    but kept the semantics (verified against the pinned binary).

    What the fork adds is more phone-home: PostHog telemetry, session ingest
    and share links to app.kilo.ai, presence, LSP downloads. Each has a
    supported kill switch, and the wrapper sets them all: the sandbox reaches
    the public web, so a path left enabled would simply phone home.
    Smoke-verified end to end against the pinned CLI: config injection, tool
    calls through the gateway, and JSON events on stdout.
    """

    name = KILO.name
    binary = "kilo-freeinference"
    catalog = KILO
