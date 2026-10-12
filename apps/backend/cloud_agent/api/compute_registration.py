"""The one endpoint a newly allocated compute node calls: register its SSH host keys.

The control plane must know a node's host key before its first SSH connection,
and a node that replaces an expiring one automatically has nobody to verify it
by hand. So the node's boot script reports its keys here, over HTTPS it checks
by certificate, with a token it received through the provider when it was
created (see ``deploy/cloudlab/cloud-agent-node.py``).

Like Host enrollment, this is unauthenticated except for that token, and the
token is the point: it is one-time, expires, is stored only as a hash, and opens
exactly one thing — recording one set of public keys for one allocation. It
cannot claim work, enroll a Host or take over a Host name; those happen later,
over the SSH session these keys authenticate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from cloud_agent.api.admin_compute import CredentialSafeRoute, get_compute_commands

if TYPE_CHECKING:
    from cloud_agent.application.compute_resources import ComputeCommands

router = APIRouter(prefix="/v1/compute", route_class=CredentialSafeRoute)


class NodeRegistrationRequest(BaseModel):
    """A node's SSH host public keys and the token it was created with."""

    model_config = ConfigDict(extra="forbid")

    token: SecretStr = Field(min_length=1, max_length=256)
    host_keys: list[str] = Field(min_length=1, max_length=8)


@router.post("/node-registrations")
async def register_node(
    body: NodeRegistrationRequest,
    commands: ComputeCommands = Depends(get_compute_commands),
) -> dict[str, bool]:
    """Record the node's host keys once; repeating the same keys is harmless."""
    await commands.register_node(body.token.get_secret_value(), body.host_keys)
    return {"registered": True}
