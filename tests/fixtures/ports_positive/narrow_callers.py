"""Positive control: the assembly's repository classes satisfy their callers' Protocols.

A capability whose mirror port was removed is typed in ``storage.assembly`` with
its repository class, and each caller declares the narrow Protocol it uses. A
caller behind ``Depends(get_storage)`` receives ``Any``, so nothing in the
production gate would notice that Protocol drifting from the repository.
``tests/unit/test_storage_ports_gate.py`` runs mypy over this file and fails
unless mypy accepts it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cloud_agent.api.config import ModelPreferenceReader
    from cloud_agent.api.work_insights import RecentRequests
    from cloud_agent.storage.assembly import Storage
    from cloud_agent.workers.notification_maintenance import NotificationPruning


def wired(storage: Storage) -> None:
    """Each capability is assignable to the Protocol its caller declares."""
    preferences: ModelPreferenceReader = storage.model_preferences
    requests: RecentRequests = storage.work_insights
    pruning: NotificationPruning = storage.notification_maintenance
    del preferences, requests, pruning
