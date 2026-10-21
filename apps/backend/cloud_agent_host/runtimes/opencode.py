"""OpenCode: the pinned CLI's ``serve`` behind the HTTP bridge."""

from __future__ import annotations

from cloud_agent_shared.runtime_catalog import OPENCODE

from .http_bridge import HttpBridgeRuntime


class OpencodeRuntime(HttpBridgeRuntime):
    """OpenCode, through the ``opencode-freeinference`` wrapper.

    Two verified facts shape the invocation. OpenCode ignores
    ``OPENAI_BASE_URL``, and its built-in ``openai`` provider speaks the
    Responses API; the ``opencode-freeinference`` wrapper instead declares a
    provider over the **bundled** ``@ai-sdk/openai-compatible`` package
    (chat-completions dialect, nothing downloaded at run time). And its
    startup fetch of the models.dev catalog hard-fails offline, so the
    wrapper disables it and declares the model in the config — without which
    every sandboxed run dies before the first request.
    """

    name = OPENCODE.name
    binary = "opencode-freeinference"
    catalog = OPENCODE
