"""The ``docker create`` command of a retained chat container.

:class:`ContainerCreationMixin` is the part of
:class:`~cloud_agent_host.container.chat_sandbox.ChatSandboxManager` that
composes the one argv a chat container is created with: its identity labels,
the unprivileged user and security profile, the private tmpfs mounts, the
limits, the inert PID 1, and the operator's ``AGENT_SANDBOX_EXTRA_ARGS`` after
refusing any that would undo those.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.container.chat_sandbox_images import require_create_image
from cloud_agent_host.container.chat_sandbox_models import (
    ACTIVATION_TMPFS,
    CHAT_CHECKPOINT_LABEL,
    CHAT_ENVIRONMENT_SNAPSHOT_LABEL,
    CHAT_NAMESPACE_LABEL,
    CHAT_WORKDIR,
    ENVIRONMENT_TEST_LABEL,
    MEMBER_TMPFS_ROOT,
    MEMBER_UID_BASE,
    ChatSandboxSpec,
)
from cloud_agent_host.container.sandbox import KATA_RUNTIME, ContainerBackend, SandboxError
from cloud_agent_host.environment_process import ACTION_UIDS, ENVIRONMENT_UIDS
from cloud_agent_host.network.chat_sandbox_network import CHAT_BOOTSTRAP_NETWORK, CHAT_OFFLINE_DNS
from cloud_agent_host.observe import guest_observer

if TYPE_CHECKING:
    import builtins


# Kata exec cannot acquire capabilities outside the container's bounding set.
# Only the fixed guest-root helpers use these: tracing, self-test UID drops,
# socket ownership, and reading/signalling member process receipts.
KATA_HELPER_CAPABILITIES = (
    "BPF",
    "PERFMON",
    "SETUID",
    "SETGID",
    "CHOWN",
    "DAC_OVERRIDE",
    "KILL",
    "SYS_PTRACE",
)
# ``AGENT_SANDBOX_EXTRA_ARGS`` follow the platform's own ``docker create``
# flags, so a later spelling would win. These would undo what those flags
# establish: the chat's identity and restart behaviour, a secret-free
# environment, state in the writable layer, the unprivileged user and its
# security profile, the one guarded network and resolver, or the limits.
_REFUSED_EXTRA_ARGS = frozenset(
    {
        "--rm",
        "--name",
        "--label",
        "--label-file",
        "--restart",
        "--entrypoint",
        "--init",
        "--env",
        "--env-file",
        "--mount",
        "--volume",
        "--volumes-from",
        "--tmpfs",
        "--storage-opt",
        "--read-only",
        "--user",
        "--group-add",
        "--privileged",
        "--cap-add",
        "--security-opt",
        "--runtime",
        "--userns",
        "--cgroupns",
        "--cgroup-parent",
        "--sysctl",
        "--device",
        "--device-cgroup-rule",
        "--gpus",
        "--pid",
        "--ipc",
        "--uts",
        "--network",
        "--network-alias",
        "--link",
        "--ip",
        "--ip6",
        "--mac-address",
        "--dns",
        "--add-host",
        "--publish",
        "--publish-all",
        "--memory",
        "--memory-swap",
        "--memory-reservation",
        "--kernel-memory",
        "--oom-kill-disable",
        "--cpus",
        "--cpu-period",
        "--cpu-quota",
        "--cpu-shares",
        "--cpu-rt-period",
        "--cpu-rt-runtime",
        "--pids-limit",
    }
)
# Docker's short flags, as the long options they spell, and the hidden aliases
# it still accepts. A short flag that takes a value ends a combined group such
# as ``-itu0``: the rest of the token is that value.
_EXTRA_ARG_SHORT_FLAGS = {
    "a": "--attach",
    "c": "--cpu-shares",
    "e": "--env",
    "h": "--hostname",
    "i": "--interactive",
    "l": "--label",
    "m": "--memory",
    "p": "--publish",
    "P": "--publish-all",
    "t": "--tty",
    "u": "--user",
    "v": "--volume",
    "w": "--workdir",
}
_EXTRA_ARG_SHORT_SWITCHES = frozenset("iPt")
_EXTRA_ARG_ALIASES = {"--net": "--network", "--net-alias": "--network-alias"}


def _extra_arg_options(token: str) -> list[str]:
    """Name the options one extra-argument token sets, as Docker reads it.

    A token that is not an option -- the value of the one before it -- sets
    none.
    """
    if token.startswith("--"):
        option = token.split("=", 1)[0]
        return [_EXTRA_ARG_ALIASES.get(option, option)]
    if not token.startswith("-") or token == "-":
        return []
    options: list[str] = []
    for letter in token[1:]:
        options.append(_EXTRA_ARG_SHORT_FLAGS.get(letter, f"-{letter}"))
        if letter not in _EXTRA_ARG_SHORT_SWITCHES:
            break
    return options


# PID 1 contains no chat credential and owns no chat state outside the
# container writable layer. A reconnectable protocol bridge may later replace
# this inert process as a latency optimization; continuity does not depend on
# it. Docker's ``--init`` owns signal forwarding/reaping, while one never-
# respawned child avoids manufacturing a same-UID process event every hour.

_INIT_COMMAND = (
    'trap \'kill "$child" 2>/dev/null; wait "$child"; exit 0\' TERM INT; '
    'sleep 2147483647 & child=$!; wait "$child"'
)


class ContainerCreationMixin:
    """Compose a chat container's ``docker create`` argv; part of ``ChatSandboxManager``."""

    backend: ContainerBackend

    def build_create_command(
        self,
        spec: ChatSandboxSpec,
        *,
        image: str | None = None,
        checkpoint_id: str | None = None,
        environment_snapshot: tuple[str, str] | None = None,
    ) -> builtins.list[str]:
        """Compose a no-secret, no-chat-volume ``docker create`` command.

        ``checkpoint_id`` recreates a moved group: the image is that
        checkpoint's local image and nothing else, and the container records
        which checkpoint it came from. ``environment_snapshot`` (owner hash,
        snapshot id) creates a new group from an installation snapshot, the
        same way.
        """
        self._validate_extra_args()
        require_create_image(
            spec, image, checkpoint_id=checkpoint_id, environment_snapshot=environment_snapshot
        )
        argv = [
            self.backend.docker_binary,
            "create",
            "--name",
            spec.container_name,
            "--init",
        ]
        labels = {
            **spec.docker_labels(),
            CHAT_NAMESPACE_LABEL: self.backend.sandbox_namespace,
        }
        if checkpoint_id is not None:
            labels[CHAT_CHECKPOINT_LABEL] = checkpoint_id
        if environment_snapshot is not None:
            labels[CHAT_ENVIRONMENT_SNAPSHOT_LABEL] = ":".join(environment_snapshot)
            labels[ENVIRONMENT_TEST_LABEL] = ""
        for key, value in sorted(labels.items()):
            argv += ["--label", f"{key}={value}"]
        argv += [
            "--workdir",
            CHAT_WORKDIR,
            "--user",
            f"{self.backend.uid}:{self.backend.gid}",
            # Docker retains the create-time network as immutable primary
            # metadata even after disconnect. Use its daemon-stable bridge as
            # a dormant anchor, then attach the group bridge and disconnect
            # this one while the container is still stopped. No untrusted code
            # runs until start() verifies the group bridge is the sole network.
            "--network",
            CHAT_BOOTSTRAP_NETWORK,
            # Docker resolves relay aliases through 127.0.0.11 and forwards
            # every other name to this server from the container's namespace:
            # the deployment's one resolver, the only DNS the host's rules let
            # a group reach. A Kata guest is given the same server directly.
            "--dns",
            (
                str(self.backend.sandbox_network.dns_server)
                if self.backend.sandbox_network is not None
                else CHAT_OFFLINE_DNS
            ),
            "--memory",
            spec.memory_limit,
            "--cpus",
            spec.cpu_limit,
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "512",
            "--stop-timeout",
            str(spec.stop_timeout_s),
            "--tmpfs",
            (
                f"{ACTIVATION_TMPFS}:rw,nosuid,nodev,noexec,mode=0700,"
                f"uid={self.backend.uid},gid={self.backend.gid}"
            ),
        ]
        if spec.disk_limit_mib:
            # Docker enforces this against the writable layer. Unsupported
            # drivers/backing filesystems reject creation; never retry without
            # the requested quota or pretend it was applied.
            argv += ["--storage-opt", f"size={spec.disk_limit_mib}M"]
        for uid in (*ENVIRONMENT_UIDS, *ACTION_UIDS):
            argv += [
                "--tmpfs",
                f"/run/agent-environments/{uid}:rw,nosuid,nodev,noexec,mode=0700,uid={uid},gid={self.backend.gid}",
            ]
        for slot_id in range(spec.resident_slot_capacity):
            uid = MEMBER_UID_BASE + slot_id
            argv += [
                "--tmpfs",
                (
                    f"{MEMBER_TMPFS_ROOT}/{slot_id}:rw,nosuid,nodev,noexec,"
                    f"mode=0700,uid={uid},gid={self.backend.gid}"
                ),
            ]
        if self.backend.runtime == KATA_RUNTIME:
            # The eBPF program is loaded by a trusted root helper *inside* the
            # Kata guest.  Its journal is root-only and ephemeral; agent UIDs
            # cannot read, forge, or retain it across a guest restart.
            #
            # Kata limits even privileged exec to this bounding set. The
            # ordinary member exec path explicitly sets no-new-privileges;
            # Kata does not inherit PID 1's setting for docker exec.
            for capability in KATA_HELPER_CAPABILITIES:
                argv += ["--cap-add", capability]
            argv += [
                "--tmpfs",
                f"{guest_observer.GUEST_OBSERVER_ROOT}:rw,nosuid,nodev,noexec,mode=0700,uid=0,gid=0",
            ]
        if self.backend.runtime:
            argv += ["--runtime", self.backend.runtime]
        argv += self.backend.extra_args
        argv += [
            "--entrypoint",
            "/bin/sh",
            image or spec.sandbox_image_digest or self.backend.image,
            "-c",
            _INIT_COMMAND,
        ]
        return argv

    def _validate_extra_args(self) -> None:
        """Reject options that would defeat retained ownership, secret or isolation rules.

        Options are read the way Docker reads them: a long option with or
        without ``=value`` or through a hidden alias, a short flag alone or
        inside a combined group. Only the option is named in the refusal,
        never its value, which may be a secret.
        """
        for token in self.backend.extra_args:
            for option in _extra_arg_options(token):
                if option in _REFUSED_EXTRA_ARGS:
                    raise SandboxError(
                        f"AGENT_SANDBOX_EXTRA_ARGS option {option!r} is unsafe for retained chats"
                    )


__all__ = ["KATA_HELPER_CAPABILITIES", "ContainerCreationMixin"]
