"""The runtime settings this host applies from the admin settings page.

The control plane answers every heartbeat with the values an admin set for this
host (see :mod:`cloud_agent_shared.host_settings`). This module holds them
beside the host's legacy bootstrap values. Once the first answer arrives, every
setting uses an admin value or its built-in default; clearing a value never
reactivates the host's old environment variable.

Two processes apply them. The runner hands its share to each turn as it starts
it, so a turn keeps the values it began with. The workspace broker is a separate
container with its own environment, so the runner tells it its share after every
beat and records what the broker says it applied; a broker that restarted on its
own ``.env`` is corrected by the next beat, at most one interval later.

What the host reports back is what it applies, not what it was sent: the
runner's share as resolved, and the broker's as the broker confirmed it. A
broker that did not answer leaves its settings out of the report, so the admin
page shows them as not yet applied rather than assuming.

Sandbox archive policy is deliberately stricter than those tuning values. It
has no environment fallback: the runner must deliver one complete DB revision
to the broker, and claims remain blocked while that confirmation is unknown.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from cloud_agent_shared.host_settings import (
    BROKER_SETTING_KEYS,
    RUNNER_SETTING_KEYS,
    admin_values,
    default_values,
    reported_values,
    resolve,
)
from cloud_agent_shared.sandbox_archive_settings import SandboxArchivePolicy

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

#: Sends the broker its admin values and returns its answer:
#: ``{"applied": {...}, "local": {...}}``.
BrokerPush = Callable[[dict[str, int]], dict[str, Any]]
ArchiveBrokerPush = Callable[[dict[str, Any]], dict[str, Any]]


class RuntimeSettings:
    """What this host applies for each runtime setting, and what it reports."""

    def __init__(self, local: Mapping[str, float], *, broker: BrokerPush | None = None) -> None:
        """Start from the runner's own values; ``broker`` is ``None`` with no broker."""
        self._local = {key: value for key, value in local.items() if key in RUNNER_SETTING_KEYS}
        self._broker = broker
        self._lock = threading.Lock()
        self._admin: dict[str, int] = {}
        self._broker_applied: dict[str, int | float] = {}
        self._broker_local: dict[str, int | float] = {}
        self._broker_error: str | None = None
        self._adopted = threading.Event()

    @property
    def adopted(self) -> bool:
        """Whether a heartbeat answer has been applied since this process started.

        Until one has, every setting is this host's own, and a turn claimed now
        would run without the values an admin set.
        """
        return self._adopted.is_set()

    def apply(self, admin: Mapping[str, Any]) -> bool:
        """Adopt the admin values from a heartbeat answer and hand the broker its share.

        The runner's share is in force before the broker is asked: a broker
        that is slow to answer must not hold up the values the next turn runs
        with. Returns whether the report changed, so the caller can send it now
        rather than an interval later. Never raises: a broker that cannot be
        reached costs this host its broker settings' confirmation, not its
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
        if self._broker is None:
            return changed
        broker_applied: dict[str, int | float] = {}
        broker_local: dict[str, int | float] = {}
        error: str | None = None
        try:
            answer = self._broker(
                {**default_values(BROKER_SETTING_KEYS), **admin_values(admin, BROKER_SETTING_KEYS)}
            )
            broker_applied = reported_values(answer.get("applied")) or {}
            broker_local = reported_values(answer.get("local")) or {}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        with self._lock:
            changed = changed or (self._broker_applied, self._broker_local) != (
                broker_applied,
                broker_local,
            )
            self._broker_applied = broker_applied
            self._broker_local = broker_local
            previous_error, self._broker_error = self._broker_error, error
        if error is not None and error != previous_error:
            logger.warning(
                "agent_host_settings_broker_unconfirmed",
                extra={"event": "agent_host_settings_broker_unconfirmed", "error": error},
            )
        return changed

    def turn_settings(self) -> dict[str, float]:
        """The runner's share, resolved: what the next turn will run with."""
        with self._lock:
            return resolve(self._admin, self._local)

    def report(self) -> tuple[dict[str, int | float], dict[str, int | float]]:
        """``(applied, local)`` for the next heartbeat.

        A setting both processes apply is in force only once both agree. It is
        reported at the runner's value when the broker confirmed the same one,
        at the broker's when it confirmed another, and not at all when the
        broker has not answered — so the page keeps showing it as applying
        while the broker still sweeps with its old value. With no broker at
        all, the runner's value is the whole truth. The runner's own value
        wins in ``local``: it is the one it reports when a turn settles.
        """
        with self._lock:
            runner = resolve(self._admin, self._local)
            applied: dict[str, int | float] = {**self._broker_applied, **runner}
            if self._broker is not None:
                for key in RUNNER_SETTING_KEYS & BROKER_SETTING_KEYS:
                    confirmed = self._broker_applied.get(key)
                    if confirmed is None:
                        applied.pop(key, None)
                    elif confirmed != runner.get(key):
                        applied[key] = confirmed
            local = {**self._broker_local, **self._local}
        return reported_values(applied) or {}, reported_values(local) or {}


class SandboxArchiveRuntimeSettings:
    """Relay the desired DB archive policy and report exact broker confirmation.

    The full policy is deliberately re-offered after every answered heartbeat,
    even when its revision did not change. The broker keeps policy in memory;
    re-offering is how a broker restart is repaired without a host restart.
    """

    def __init__(self, broker: ArchiveBrokerPush | None) -> None:
        self._broker = broker
        self._lock = threading.Lock()
        self._applied_revision: int | None = None
        self._ready = threading.Event()

    @property
    def ready(self) -> bool:
        """Whether this broker confirmed the first complete desired policy."""
        return self._ready.is_set()

    def apply(self, settings: Mapping[str, Any]) -> bool:
        """Offer a complete policy to the broker and verify its exact revision."""
        policy = SandboxArchivePolicy.from_mapping(settings)
        before = self.invalidate()
        if self._broker is None:
            raise RuntimeError("workspace broker is unavailable for sandbox archive policy")
        answer = self._broker(policy.as_dict())
        applied = answer.get("applied_revision")
        if isinstance(applied, bool) or not isinstance(applied, int):
            raise RuntimeError("workspace broker returned no sandbox archive policy revision")
        if applied != policy.revision:
            raise RuntimeError(
                "workspace broker applied a different sandbox archive policy revision"
            )
        with self._lock:
            self._applied_revision = applied
            self._ready.set()
        return before != applied

    def invalidate(self) -> int | None:
        """Forget confirmation when a beat omits policy or a re-offer begins."""
        with self._lock:
            before = self._applied_revision
            # Re-offering is also the broker liveness check. A confirmation
            # from a broker process that has since restarted is no longer a
            # confirmation, even when the desired revision did not change.
            self._applied_revision = None
            self._ready.clear()
            return before

    def report(self) -> int | None:
        """Return the last exact revision confirmed by the broker."""
        with self._lock:
            return self._applied_revision


__all__ = [
    "ArchiveBrokerPush",
    "BrokerPush",
    "RuntimeSettings",
    "SandboxArchiveRuntimeSettings",
]
