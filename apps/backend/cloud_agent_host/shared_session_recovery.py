"""Owned recovery keeps slow final archives off observational request workers."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

from cloud_agent_host.sandbox import SandboxError
from cloud_agent_host.session_lifecycle import SessionBusy

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxInspection
    from cloud_agent_host.shared_session_supervisor import (
        SharedChatSessionSupervisor,
        SharedSessionDescriptor,
        _LifecycleCheck,
    )

logger = logging.getLogger(__name__)


class SharedSessionRecovery:
    """Drain finalization before closing archive services, never on ASGI's pool.

    The supervisor reserves each exact group's lifecycle check before submit.
    That reservation deduplicates listings and fences mutations until recovery
    finishes. Explicit reconciliation and startup stay synchronous.
    """

    def __init__(self, owner: SharedChatSessionSupervisor) -> None:
        self._owner = owner
        self._lock = threading.Lock()
        self._executor: ThreadPoolExecutor | None = None
        self._closed = False

    def start(self) -> None:
        """Allow recovery again when the supervisor restarts."""
        with self._lock:
            self._closed = False

    def quiesce(self) -> None:
        """Seal admission and drain every reserved group before returning."""
        with self._lock:
            self._closed = True
            executor = self._executor
        if executor is not None:
            # Do not cancel queued checks or abandon archive services in use.
            executor.shutdown(wait=True)
        with self._lock:
            self._executor = None

    def reconcile(
        self,
        check: _LifecycleCheck,
        inspection: ChatSandboxInspection,
        *,
        discovered: bool,
        read_marker: bool,
        background: bool,
    ) -> list[SharedSessionDescriptor] | None:
        """Run or enqueue one already-reserved exact-group recovery."""
        if not background:
            return self._recover(check, inspection, discovered, read_marker)
        try:
            with self._lock:
                if self._closed:
                    raise SessionBusy("shared sandbox recovery is shutting down")
                if self._executor is None:
                    self._executor = ThreadPoolExecutor(
                        max_workers=1, thread_name_prefix="shared-sandbox-recovery"
                    )
                # At most one queued item per reserved group, regardless of
                # how many heartbeats request observations while it uploads.
                self._executor.submit(
                    self._recover_background, check, inspection, discovered, read_marker
                )
        except BaseException:
            self._finish(check)
            raise
        return None

    def _recover_background(
        self,
        check: _LifecycleCheck,
        inspection: ChatSandboxInspection,
        discovered: bool,
        read_marker: bool,
    ) -> None:
        try:
            self._recover(check, inspection, discovered, read_marker)
        except Exception:
            logger.exception("shared sandbox background recovery failed")

    def _recover(
        self,
        check: _LifecycleCheck,
        inspection: ChatSandboxInspection,
        discovered: bool,
        read_marker: bool,
    ) -> list[SharedSessionDescriptor] | None:
        try:
            return self._owner._reconcile_checked_group(
                check, inspection, discovered=discovered, read_marker=read_marker
            )
        except SandboxError as exc:
            # A changed/lost group retains its last observation and can retry
            # next listing; it must not cost other groups their observations.
            logger.warning(
                "shared sandbox group was not reconciled",
                extra={
                    "event": "shared_sandbox_reconcile_group_failed",
                    "sandbox_id": check.spec.sandbox_id,
                    "generation": check.spec.generation,
                    "detail": str(exc)[:300],
                },
            )
            return None
        finally:
            self._finish(check)

    def _finish(self, check: _LifecycleCheck) -> None:
        with self._owner._lock:
            self._owner._end_lifecycle_check_locked(check)
