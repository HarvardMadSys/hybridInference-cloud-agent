"""Postgres compute inventory and recoverable operations.

All provider/SSH calls belong to the application layer. Every external result
may be fenced against its worker lease, and every destructive decision is
rechecked in the same transaction as its irreversible commit point.

The repository is one class composed of one mixin per aggregate, each in its
own module; ``_common`` holds the pool and the steps they share.
"""

from __future__ import annotations

from cloud_agent.storage.repositories.compute.allocations import AllocationsMixin
from cloud_agent.storage.repositories.compute.autoscaling import AutoscalingMixin
from cloud_agent.storage.repositories.compute.connections import ConnectionsMixin
from cloud_agent.storage.repositories.compute.holds import HoldsMixin
from cloud_agent.storage.repositories.compute.move_out import MoveOutMixin
from cloud_agent.storage.repositories.compute.node_registration import NodeRegistrationMixin
from cloud_agent.storage.repositories.compute.operations import OperationsMixin
from cloud_agent.storage.repositories.compute.profiles import ProfilesMixin
from cloud_agent.storage.repositories.compute.retirement import RetirementMixin
from cloud_agent.storage.repositories.compute.rotation import RotationMixin


class PostgresComputeResourceRepository(
    ProfilesMixin,
    ConnectionsMixin,
    AllocationsMixin,
    OperationsMixin,
    AutoscalingMixin,
    NodeRegistrationMixin,
    HoldsMixin,
    MoveOutMixin,
    RotationMixin,
    RetirementMixin,
):
    """One durable identity for each resource instance and operation intent."""
