"""The runtime settings this host applies from the admin settings page.

The control plane answers every heartbeat with the values an admin set for this
host (see :mod:`cloud_agent_shared.host_settings`). This module holds them
beside the host's legacy bootstrap values. Once the first answer arrives, every
setting uses an admin value or its built-in default; clearing a value never
reactivates the host's old environment variable.

Two processes apply them. The runner hands its share to each turn as it starts
it, so a turn keeps the values it began with. The sandbox manager is a separate
container with its own environment, so the runner tells it its share after every
beat and records what the sandbox manager says it applied; a sandbox manager that restarted on its
own ``.env`` is corrected by the next beat, at most one interval later.

What the host reports back is what it applies, not what it was sent: the
runner's share as resolved, and the sandbox manager's as the sandbox manager confirmed it. A
sandbox manager that did not answer leaves its settings out of the report, so the admin
page shows them as not yet applied rather than assuming.

Sandbox archive policy is deliberately stricter than those tuning values. It
has no environment fallback: the runner must deliver one complete DB revision
to the sandbox manager, and claims remain blocked while that confirmation is unknown.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.host_settings import (
    RUNNER_SETTING_KEYS,
    SANDBOX_MANAGER_SETTING_KEYS,
    admin_values,
    default_values,
    reported_values,
    resolve,
)
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: Sends the sandbox manager its admin values and returns its answer:
#: ``{"applied": {...}, "local": {...}}``.
SandboxManagerPush = Callable[[dict[str, int]], dict[str, Any]]
ArchiveSandboxManagerPush = Callable[[dict[str, Any]], dict[str, Any]]


class RuntimeSettings:
    """What this host applies for each runtime setting, and what it reports."""

    def __init__(
        self, local: Mapping[str, float], *, sandbox_manager: SandboxManagerPush | None = None
    ) -> None:
        """Start from the runner's own values; ``sandbox_manager`` is ``None`` with no sandbox manager."""
        self._local = {key: value for key, value in local.items() if key in RUNNER_SETTING_KEYS}
        self._sandbox_manager = sandbox_manager
        self._lock = threading.Lock()
        self._admin: dict[str, int] = {}
        self._sandbox_manager_applied: dict[str, int | float] = {}
        self._sandbox_manager_local: dict[str, int | float] = {}
        self._sandbox_manager_error: str | None = None
        self._adopted = threading.Event()

    @property
    def adopted(self) -> bool:
        """Whether a heartbeat answer has been applied since this process started.

        Until one has, every setting is this host's own, and a turn claimed now
        would run without the values an admin set.
        """
        return self._adopted.is_set()

    def apply(self, admin: Mapping[str, Any]) -> bool:
        """Adopt the admin values from a heartbeat answer and hand the sandbox manager its share.

        The runner's share is in force before the sandbox manager is asked: a sandbox manager
        that is slow to answer must not hold up the values the next turn runs
        with. Returns whether the report changed, so the caller can send it now
        rather than an interval later. Never raises: a sandbox manager that cannot be
        reached costs this host its sandbox manager settings' confirmation, not its
        heartbeat.
        """
        runner_admin = {
            **default_values(RUNNER_SETTING_KEYS),
            **admin_values(admin, RUNNER_SETTING_KEYS),
        }
        with self._lock:
            was = resolve(self._admin, self._local)
            self._admin = runner_admin
            now = resolve(self._admin, self._local)
            changed = was != now
        self._adopted.set()
        if was != now:
            logger.info(
                "agent_host_settings_changed",
                extra={"event": "agent_host_settings_changed", "was": was, "now": now},
            )
        if self._sandbox_manager is None:
            return changed
        sandbox_manager_applied: dict[str, int | float] = {}
        sandbox_manager_local: dict[str, int | float] = {}
        error: str | None = None
        try:
            answer = self._sandbox_manager(
                {
                    **default_values(SANDBOX_MANAGER_SETTING_KEYS),
                    **admin_values(admin, SANDBOX_MANAGER_SETTING_KEYS),
                }
            )
            sandbox_manager_applied = reported_values(answer.get("applied")) or {}
            sandbox_manager_local = reported_values(answer.get("local")) or {}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            changed = changed or (self._sandbox_manager_applied, self._sandbox_manager_local) != (
                sandbox_manager_applied,
                sandbox_manager_local,
            )
            self._sandbox_manager_applied = sandbox_manager_applied
            self._sandbox_manager_local = sandbox_manager_local
            previous_error, self._sandbox_manager_error = self._sandbox_manager_error, error
        if error is not None and error != previous_error:
            logger.warning(
                "agent_host_settings_sandbox_manager_unconfirmed",
                extra={"event": "agent_host_settings_sandbox_manager_unconfirmed", "error": error},
            )
        return changed

    def turn_settings(self) -> dict[str, float]:
        """The runner's share, resolved: what the next turn will run with."""
        with self._lock:
            return resolve(self._admin, self._local)

    def report(self) -> tuple[dict[str, int | float], dict[str, int | float]]:
        """``(applied, local)`` for the next heartbeat.

        A setting both processes apply is in force only once both agree. It is
        reported at the runner's value when the sandbox manager confirmed the same one,
        at the sandbox manager's when it confirmed another, and not at all when the
        sandbox manager has not answered — so the page keeps showing it as applying
        while the sandbox manager still sweeps with its old value. With no sandbox manager at
        all, the runner's value is the whole truth. The runner's own value
        wins in ``local``: it is the one it reports when a turn settles.
        """
        with self._lock:
            runner = resolve(self._admin, self._local)
            applied: dict[str, int | float] = {**self._sandbox_manager_applied, **runner}
            if self._sandbox_manager is not None:
                for key in RUNNER_SETTING_KEYS & SANDBOX_MANAGER_SETTING_KEYS:
                    confirmed = self._sandbox_manager_applied.get(key)
                    if confirmed is None:
                        applied.pop(key, None)
                    elif confirmed != runner.get(key):
                        applied[key] = confirmed
            local = {**self._sandbox_manager_local, **self._local}
        return reported_values(applied) or {}, reported_values(local) or {}


class SandboxArchiveRuntimeSettings:
    """Relay the desired DB archive policy and report exact sandbox manager confirmation.

    The full policy is deliberately re-offered after every answered heartbeat,
    even when its revision did not change. The sandbox manager keeps policy in memory;
    re-offering is how a sandbox manager restart is repaired without a host restart.
    """

    def __init__(self, sandbox_manager: ArchiveSandboxManagerPush | None) -> None:
        self._sandbox_manager = sandbox_manager
        self._lock = threading.Lock()
        self._applied_revision: int | None = None
        self._ready = threading.Event()

    @property
    def ready(self) -> bool:
        """Whether this sandbox manager confirmed the first complete desired policy."""
        return self._ready.is_set()

    def apply(self, settings: Mapping[str, Any]) -> bool:
        """Offer a complete policy to the sandbox manager and verify its exact revision."""
        policy = SandboxArchivePolicy.from_mapping(settings)
        before = self.invalidate()
        if self._sandbox_manager is None:
            raise RuntimeError("sandbox manager is unavailable for sandbox archive policy")
        answer = self._sandbox_manager(policy.as_sandbox_manager_dict())
        applied = answer.get("applied_revision")
        if isinstance(applied, bool) or not isinstance(applied, int):
            raise RuntimeError("sandbox manager returned no sandbox archive policy revision")
        if applied != policy.revision:
            raise RuntimeError(
                "sandbox manager applied a different sandbox archive policy revision"
            )
        with self._lock:
            self._applied_revision = applied
            self._ready.set()
        return before != applied

    def invalidate(self) -> int | None:
        """Forget confirmation when a beat omits policy or a re-offer begins."""
        with self._lock:
            before = self._applied_revision
            # Re-offering is also the sandbox manager liveness check. A confirmation
            # from a sandbox manager process that has since restarted is no longer a
            # confirmation, even when the desired revision did not change.
            self._applied_revision = None
            self._ready.clear()
            return before

    def report(self) -> int | None:
        """Return the last exact revision confirmed by the sandbox manager."""
        with self._lock:
            return self._applied_revision


__all__ = [
    "ArchiveSandboxManagerPush",
    "RuntimeSettings",
    "SandboxArchiveRuntimeSettings",
    "SandboxManagerPush",
]
