"""Assemble the optional administrator compute surface and its worker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent.application.cloudlab_inventory import CloudLabNodeInventory
from cloud_agent.application.compute_autoscale import CloudLabAutoscaler
from cloud_agent.application.compute_reconcile import ComputeReconciler
from cloud_agent.application.compute_resources import ComputeCommands
from cloud_agent.application.compute_setup import AutomaticComputeSetup
from cloud_agent.compute_config import ComputeConfiguration, ComputeSecretResolver
from cloud_agent.compute_registry import build_registry
from cloud_agent.compute_secrets import ComputeSecretStore
from cloud_agent.providers.compute.host_bootstrap import SshHostBootstrap
from cloud_agent.providers.compute.setup_tunnels import SshTunnelSetup
from cloud_agent.providers.compute.ssh import OpenSshTransport
from cloud_agent.workers.compute_resources import ComputeResourceWorker

if TYPE_CHECKING:
    import httpx

    from cloud_agent.ports.compute_resources import ComputeResourceRepository
    from cloud_agent.ports.host_pool import HostPoolRepository


def build_compute_runtime(
    *,
    repository: ComputeResourceRepository,
    host_pool: HostPoolRepository,
    http: httpx.AsyncClient,
) -> tuple[ComputeCommands, ComputeResourceWorker]:
    """Compose adapters outside the Host and HTTP layers."""
    configuration = ComputeConfiguration.from_env().with_runtime_settings()
    secrets = ComputeSecretStore(repository)
    resolver = ComputeSecretResolver(secrets, configuration)
    transport = OpenSshTransport(resolver)
    registry = build_registry(configuration, resolver=resolver, transport=transport)
    automatic_setup = AutomaticComputeSetup(
        repository=repository,
        secrets=secrets,
        resolver=resolver,
        configuration=configuration,
        tunnels=SshTunnelSetup(transport=transport),
        validate_bundle=ComputeCommands._validate_bundle,
    )
    commands = ComputeCommands(
        repository=repository,
        registry=registry,
        secrets=secrets,
        configuration=configuration,
        automatic_setup=automatic_setup,
        cloudlab_inventory=CloudLabNodeInventory(client=http),
    )
    bootstrap = SshHostBootstrap(transport=transport, resolver=resolver)
    reconciler = ComputeReconciler(commands=commands, host_pool=host_pool, bootstrap=bootstrap)
    autoscaler = CloudLabAutoscaler(repository=repository, host_pool=host_pool)
    return commands, ComputeResourceWorker(reconciler, autoscaler=autoscaler)
