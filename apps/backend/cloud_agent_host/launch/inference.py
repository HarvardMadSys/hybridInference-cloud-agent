"""The owner's model connection a member's CLI runs on."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, override

from cloud_agent_host.launch.facet import SECRET, LaunchFacet
from cloud_agent_host.member_setup.compat_helper import compat_token_env, install_compat_helper

if TYPE_CHECKING:
    from collections.abc import Mapping

    from cloud_agent_host.launch.facet import PrepareContext


@dataclass(frozen=True)
class InferenceFacet(LaunchFacet):
    """The connection's key, exact Base URL, protocol, revision, and translation or meter.

    The key is not process-fixed: a probe carries none and borrows the live
    process's. Everything else is, the source above all: it names the key's
    revision, so a rotated key never serves a turn from the old process.
    """

    # Never in a repr: the owner connection's own key.
    credential: str | None = field(repr=False, metadata=SECRET)
    base_url: str
    protocol: str
    source: str
    # The CLI's own protocol behind its compatibility helper; empty is direct.
    client_protocol: str
    # That member's loopback token on a translated plan.
    local_token: str = field(repr=False, metadata=SECRET)
    # ``"1"`` when the URL is this host's model relay and the credential a
    # residency placeholder for it (a connection the deployment provides).
    relayed: str = ""
    # A direct plan whose CLI runs behind the member's request meter
    # (``cloud_agent_host.model_passthrough``). Not a fingerprint of its own:
    # it follows from the runtime and the Base URL, both already compared.
    metered: bool = False

    @override
    def fingerprint(self) -> Mapping[str, object]:
        return {
            "inference_source": self.source,
            "inference_base_url": self.base_url,
            "inference_protocol": self.protocol,
            "inference_client_protocol": self.client_protocol,
            "inference_local_token": self.local_token,
            "inference_relayed": self.relayed,
        }

    @override
    def prepare(self, context: PrepareContext) -> None:
        if self.client_protocol or self.metered:
            # A retained container gets this host's helper, not its image's.
            install_compat_helper(context.manager, context.spec)

    @override
    def env(self) -> dict[str, str]:
        return compat_token_env(self.local_token)

    @override
    def secrets(self) -> dict[str, str]:
        values = {"inference_credential": self.credential or "", "compat_token": self.local_token}
        return {name: value for name, value in values.items() if value}
