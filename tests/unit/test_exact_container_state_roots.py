"""An exact container cannot be built with its state-mount check switched off."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from cloud_agent_host.container.chat_sandbox import CHAT_STATE_DIR, CHAT_WORKDIR, ChatSandboxManager
from cloud_agent_host.container.exact_container import ExactContainer


def test_no_state_roots_is_refused() -> None:
    with pytest.raises(ValueError, match="state roots"):
        ExactContainer(SimpleNamespace(), state_roots=())


def test_the_manager_refuses_both_chat_state_roots_as_mounts() -> None:
    manager = ChatSandboxManager(SimpleNamespace())
    assert manager.exact._state_roots == (CHAT_WORKDIR, CHAT_STATE_DIR)
