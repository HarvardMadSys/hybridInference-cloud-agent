"""What a retained chat's network proof keeps, and the checks a spawn and an install make.

Which container a bounded command execs into, and when it is inspected first,
is :class:`cloud_agent_host.exact_container.ExactContainer`'s. A spawn is
inspected first (:func:`spawn_target`), since its refusal would surface only
when it exits.

A network proof stands for a short time too (:func:`standing_network_proof`):
the exact running container it proved, unchanged and not restarted, may be
admitted on it again without repeating the guest's network sync. A proof
that fails withdraws the one before it.
"""

from __future__ import annotations

import functools
import time
from typing import TYPE_CHECKING, Concatenate, ParamSpec, TypeVar

from cloud_agent_host.chat_sandbox_models import ChatSandboxStatus, RetainedChatLost
from cloud_agent_host.exact_container import NOT_RUNNING
from cloud_agent_host.github_relay_trust import ensure_github_relay_trust
from cloud_agent_host.member_setup.install_locks import install_lock
from cloud_agent_host.sandbox import SandboxError

if TYPE_CHECKING:
    from collections.abc import Callable

    from cloud_agent_host.chat_sandbox import ChatSandboxManager
    from cloud_agent_host.chat_sandbox_models import ChatSandboxInspection, ChatSandboxSpec
    from cloud_agent_host.exact_container import ExactContainer

Proved = TypeVar("Proved")
ProofArgs = ParamSpec("ProofArgs")


def network_proof(
    method: Callable[Concatenate[ChatSandboxManager, ChatSandboxSpec, ProofArgs], Proved],
) -> Callable[Concatenate[ChatSandboxManager, ChatSandboxSpec, ProofArgs], Proved]:
    """Make a manager method that proves ``spec``'s network keep or withdraw that proof.

    One that returns proved the container it recorded; one that raises, for
    any reason, leaves no earlier proof standing.
    """

    @functools.wraps(method)
    def proving(
        manager: ChatSandboxManager,
        spec: ChatSandboxSpec,
        /,
        *args: ProofArgs.args,
        **kwargs: ProofArgs.kwargs,
    ) -> Proved:
        proved_from = time.monotonic()
        try:
            result = method(manager, spec, *args, **kwargs)
        except BaseException:
            manager.exact.withdraw_network_proof(spec)
            raise
        manager.exact.note_network_proof(spec, proved_from)
        return result

    return proving


def standing_network_proof(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    current: ChatSandboxInspection,
    *,
    max_age_s: float,
) -> float | None:
    """When the network proof ``current`` may still be admitted on began; ``None`` if none.

    ``current`` is a fresh inspection. The proof stands only for exactly the
    container it proved, still running, not restarted since (Docker's start
    time), on exactly the bridge object it was proved on and nothing else, for
    under ``max_age_s`` after it began, with no failed proof of the name since
    and no start of it still unproven. The host's rules are still required:
    the guard trusts its own verification no longer than it does for a start,
    and checks again, or raises, once that lapses. A stand-in manager that
    keeps no proofs has none standing.
    """
    exact: ExactContainer | None = getattr(manager, "exact", None)
    standing_of = getattr(exact, "standing_proof", None)
    standing = None if standing_of is None else standing_of(spec)
    if exact is None or standing is None:
        return None
    proved, proved_from = standing
    seen = (current.container_id, current.started_at, current.networks, current.network_ids)
    if (
        exact.guest_unproven(current.container_id)
        or time.monotonic() - proved_from >= max_age_s
        or not current.running
        or current.status is not ChatSandboxStatus.RUNNING
        or not current.started_at
        or seen != (proved.container_id, proved.started_at, proved.networks, proved.network_ids)
        or current.networks != (manager.network.network_for(spec),)
    ):
        return None
    manager.network.require_host_rules()
    return proved_from


def install_once(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, key: str, install: Callable[[], object]
) -> None:
    """Run ``install`` under the container's install lock, unless it already did for ``key``.

    ``key`` names a root-owned file no member can change, and ``install``
    finds it in place or puts it there, raising otherwise. Once it has, in
    the container ``spec``'s last start or network proof verified, it is not
    run there again
    (:meth:`~cloud_agent_host.exact_container.ExactContainer.note_helper`).
    The check comes before the lock, so a launch whose file is known in place
    does not queue behind a runtime or MCP install into the same container.
    """
    proven = manager.exact.proven_container(spec)
    if manager.exact.holds_helper(proven, key):
        return
    with install_lock(spec.container_name):
        install()
    manager.exact.note_helper(proven, key)


def spawn_target(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    env: dict[str, str] | None = None,
    *,
    not_running: str = NOT_RUNNING,
) -> tuple[ChatSandboxInspection, dict[str, str]]:
    """Inspect the container a process is spawned into, refusing a same-name replacement.

    Also the environment the process starts with: ``env`` over the GitHub
    relay's trust, which every process in the sandbox gets, credential or not
    (:mod:`cloud_agent_host.github_relay_trust`).
    """
    current = manager.exact.require(spec)
    proven = manager.exact.proven_container(spec)
    if proven is not None and proven.container_id != current.container_id:
        raise RetainedChatLost("chat sandbox container identity changed")
    if not current.running:
        raise SandboxError(not_running)
    return current, {**ensure_github_relay_trust(manager, spec), **(env or {})}
