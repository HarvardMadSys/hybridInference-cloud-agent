"""The owner's frozen environment binding, as one member's CLI receives it."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, override

from cloud_agent_host.chat_sandbox import member_inference_env_path
from cloud_agent_host.launch.facet import LaunchFacet
from cloud_agent_shared.environment import inference_handoff

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent_shared.environment import BoundEnvironment


@dataclass(frozen=True)
class EnvironmentFacet(LaunchFacet):
    """The binding's runtime variables, and the slot whose ``BASH_ENV`` script holds its keys."""

    environment: BoundEnvironment | None = field(repr=False)
    slot_id: int

    @override
    def fingerprint(self) -> Mapping[str, object]:
        return {"environment": self.environment}

    @cached_property
    def _handoff(self) -> tuple[dict[str, str], str]:
        if self.environment is None:
            return {}, ""
        # The CLI reads its platform credential under the same names as the
        # owner's inference credentials, so those reach only the commands it
        # runs: every bash it starts sources them from BASH_ENV.
        return inference_handoff(self.environment.values("runtime"))

    @property
    def inference_script(self) -> str:
        """The member-private ``BASH_ENV`` script with the owner's inference keys, or ``""``."""
        return self._handoff[1]

    @override
    def env(self) -> dict[str, str]:
        if self.environment is None:
            return {}
        from cloud_agent_host.environment_process import tool_environment

        runtime_values, inference_script = self._handoff
        env = {**tool_environment(), **runtime_values}
        if inference_script:
            env["BASH_ENV"] = member_inference_env_path(self.slot_id)
        return env

    @override
    def secrets(self) -> dict[str, str]:
        return self.environment.secrets() if self.environment is not None else {}
