"""An archive policy built from the individual hooks a supervisor test needs."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cloud_agent_host.group_archive_policy import NullArchivePolicy
from cloud_agent_host.shared_session_supervisor import SharedChatSessionSupervisor

Hook = Callable[..., Any]


@dataclass(frozen=True)
class _Hooks:
    """Each missing hook behaves as that transition has no archive."""

    on_stopped: Hook | None
    on_prepare_final: Hook | None
    on_invalidate_final: Hook | None
    on_markerless_final: Hook | None
    on_prepare_start: Hook | None
    on_started: Hook | None
    on_restore_checkpoint: Hook | None
    on_environment_snapshot: Hook | None
    on_checkpoint_seq: Hook | None

    @property
    def archives_finals(self) -> bool:
        return self.on_stopped is not None

    @property
    def restores_checkpoints(self) -> bool:
        return self.on_restore_checkpoint is not None

    def prepare_final(self, spec, inspection) -> None:
        if self.on_prepare_final is not None:
            self.on_prepare_final(spec, inspection)

    def stopped(self, spec, inspection, members, *, start_seq) -> None:
        if self.on_stopped is not None:
            self.on_stopped(spec, inspection, members)

    def invalidate_final(self, spec, inspection) -> bool:
        if self.on_invalidate_final is None:
            return False
        self.on_invalidate_final(spec, inspection)
        return True

    def markerless_final(self, spec, inspection) -> bool:
        if self.on_markerless_final is None:
            return False
        return self.on_markerless_final(spec, inspection)

    def prepare_start(self, spec, inspection) -> bool:
        if self.on_prepare_start is None:
            return False
        self.on_prepare_start(spec, inspection)
        return True

    def started(self, spec, inspection) -> None:
        if self.on_started is not None:
            self.on_started(spec, inspection)

    def restore_checkpoint(self, sandbox_id, restore) -> str:
        if self.on_restore_checkpoint is None:
            return NullArchivePolicy().restore_checkpoint(sandbox_id, restore)
        return self.on_restore_checkpoint(sandbox_id, restore)

    def environment_snapshot(self, snapshot) -> str | None:
        if self.on_environment_snapshot is None:
            return None
        return self.on_environment_snapshot(snapshot)

    def checkpoint_seq(self, spec, inspection) -> int | None:
        if self.on_checkpoint_seq is None:
            return None
        return self.on_checkpoint_seq(spec, inspection)


def archive_hooks(
    *,
    stopped: Hook | None = None,
    prepare_final: Hook | None = None,
    invalidate_final: Hook | None = None,
    markerless_final: Hook | None = None,
    prepare_start: Hook | None = None,
    started: Hook | None = None,
    restore_checkpoint: Hook | None = None,
    environment_snapshot: Hook | None = None,
    checkpoint_seq: Hook | None = None,
) -> _Hooks:
    """A policy that calls exactly the hooks given, as the supervisor's old keywords did."""
    return _Hooks(
        on_stopped=stopped,
        on_prepare_final=prepare_final,
        on_invalidate_final=invalidate_final,
        on_markerless_final=markerless_final,
        on_prepare_start=prepare_start,
        on_started=started,
        on_restore_checkpoint=restore_checkpoint,
        on_environment_snapshot=environment_snapshot,
        on_checkpoint_seq=checkpoint_seq,
    )


_HOOK_NAMES = frozenset(inspect.signature(archive_hooks).parameters)


def hooked_supervisor(manager: Any, **options: Any) -> SharedChatSessionSupervisor:
    """A supervisor whose policy is the :func:`archive_hooks` named among ``options``."""
    hooks = {name: options.pop(name) for name in _HOOK_NAMES & options.keys()}
    return SharedChatSessionSupervisor(manager, archive_policy=archive_hooks(**hooks), **options)
