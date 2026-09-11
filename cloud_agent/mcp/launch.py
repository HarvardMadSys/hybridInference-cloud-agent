"""Starting a stdio server in a managed worker, reproducibly and without a shell.

Two things make a managed stdio server different from the same configuration on
somebody's laptop.

**Nothing resolves at run time that can be resolved at install time.** ``npx
some-server`` downloads whatever ``latest`` means today, in the environment
that holds a credential, and produces a different program every week. So a
launch plan is split in two: an *install* step that fixes an exact package
version, and a *run* step that starts it from what was installed. The installed
artifact is what runs, and a version change is a connection edit -- which is
why the plan carries the resolved version rather than looking it up again.

**No shell, anywhere.** ``argv`` is executed directly. That is not a hardening
measure on top of escaping; it is the removal of the thing that needs escaping.
The remaining risk is the program itself, which is why the user's package is
never treated as trusted code: it runs in a worker that holds one connection's
secrets, no host socket, no control-plane credentials, and only the mounts the
owner approved.

The module is pure: it decides what would run, and refuses what it cannot
express safely. Executing the plan belongs to the worker, not here.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

from cloud_agent.mcp.models import ExecutionLocation, LaunchSpec, Mount

#: Interpreters whose argument vector is ``<runner> <package> [args...]`` and
#: whose package therefore has to be pinned.
PACKAGE_RUNNERS: Mapping[str, str] = {
    "npx": "npm",
    "npm": "npm",
    "pnpm": "npm",
    "yarn": "npm",
    "bunx": "npm",
    "uvx": "pypi",
    "pipx": "pypi",
    "docker": "oci",
    "deno": "deno",
}

#: Programs that would put a shell back between us and the server, undoing the
#: reason ``argv`` is used in the first place.
SHELL_COMMANDS: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "ksh",
        "fish",
        "csh",
        "tcsh",
        "cmd",
        "cmd.exe",
        "powershell",
        "pwsh",
    }
)

#: Interpreters a *managed* worker is willing to start. A user connector is not
#: bounded by this list -- it runs on the user's own machine, under their own
#: policy -- but the platform's shared worker is, because there the command is
#: a program the platform runs beside other connections.
MANAGED_ALLOWED_COMMANDS: frozenset[str] = frozenset(
    {
        "npx",
        "node",
        "uvx",
        "uv",
        "python",
        "python3",
        "deno",
        "bun",
        "docker",
        "pipx",
    }
)

#: A package reference: ``name``, ``@scope/name``, ``name@1.2.3``,
#: ``name==1.2.3``, ``name>=1,<2``. Compared as text; the installer decides
#: what it means, and this only decides whether a version is present at all.
_PINNED_NPM = re.compile(r"^(?P<name>(?:@[^/@]+/)?[^@/][^@]*)@(?P<version>[^@]+)$")
_PINNED_PYPI = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?:==|>=|~=)\s*(?P<version>.+)$")


class LaunchPlanError(ValueError):
    """Raised when a launch spec cannot be expressed as a managed plan."""

    def __init__(self, message: str, *, kind: str = "launch_failed") -> None:
        """Record the diagnosis kind alongside the message."""
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ManagedLaunchPolicy:
    """What a deployment's managed worker is willing to run."""

    allowed_commands: frozenset[str] = MANAGED_ALLOWED_COMMANDS
    #: The environment a package is installed in, separate from the one that
    #: holds business secrets. The worker mounts it read-only at run time.
    install_root: str = "/opt/mcp-installs"
    #: Where a worker may be told to place an approved mount.
    mount_root: str = "/workspace/mcp"
    #: Whether the worker may reach the network at all while a server runs.
    #: ``package-install-only`` is the default: the install step needs the
    #: registry, the running server is expected to talk to its own service over
    #: the relay rather than directly.
    network: Literal["none", "package-install-only", "egress"] = "package-install-only"
    max_mounts: int = 8
    max_env_entries: int = 64
    max_argv_bytes: int = 8192


@dataclass(frozen=True)
class InstallStep:
    """The one command that fixes what will run, and the version it fixes."""

    argv: tuple[str, ...]
    ecosystem: str
    package: str
    version: str

    def as_dict(self) -> dict[str, object]:
        """Render for the install record and for the confirmation screen."""
        return {
            "argv": list(self.argv),
            "ecosystem": self.ecosystem,
            "package": self.package,
            "version": self.version,
        }


@dataclass(frozen=True)
class LaunchPlan:
    """What a managed worker is told: install once, then run this argv."""

    connection_id: str
    run_argv: tuple[str, ...]
    install: InstallStep | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    mounts: tuple[Mount, ...] = ()
    cwd: str | None = None
    network: str = "package-install-only"
    #: Stable digest over everything that would make a different program run.
    #: The worker compares it before reusing an installed tree.
    plan_digest: str = ""

    def as_dict(self) -> dict[str, object]:
        """Render the plan, with no secret values in it."""
        return {
            "connection_id": self.connection_id,
            "run_argv": list(self.run_argv),
            "install": self.install.as_dict() if self.install else None,
            "env_names": sorted(self.env),
            "mounts": [
                {"source": mount.source, "target": mount.target, "read_only": mount.read_only}
                for mount in self.mounts
            ],
            "cwd": self.cwd,
            "network": self.network,
            "plan_digest": self.plan_digest,
        }


def split_package_reference(reference: str) -> tuple[str, str | None]:
    """Split ``name@version`` / ``name==version`` into its two halves."""
    candidate = reference.strip()
    if not candidate:
        raise LaunchPlanError("a package reference cannot be empty")
    if candidate.startswith("http://") or candidate.startswith("https://"):
        # A tarball URL is not a package name and carries no version we can
        # pin, so it is refused rather than installed by surprise.
        raise LaunchPlanError(
            "install this server from a package name rather than a URL so its version can be pinned"
        )
    for pattern in (_PINNED_NPM, _PINNED_PYPI):
        match = pattern.match(candidate)
        if match:
            return match.group("name"), match.group("version").strip()
    return candidate, None


def _command_name(command: str) -> str:
    """The program's basename, so ``/usr/bin/npx`` is still ``npx``."""
    return command.replace("\\", "/").rsplit("/", 1)[-1].lower()


def _ecosystem(command: str) -> str | None:
    """Which package ecosystem a runner installs from, if any."""
    return PACKAGE_RUNNERS.get(_command_name(command))


def _install_argv(ecosystem: str, package: str, version: str) -> tuple[str, ...]:
    """The install command for one ecosystem.

    ``--ignore-scripts`` for npm and ``--no-deps`` for pip are deliberate: the
    install step runs where a credential is about to live, so a package's
    lifecycle hooks are not given a chance to run there.
    """
    pinned = f"{package}@{version}" if ecosystem == "npm" else f"{package}=={version}"
    match ecosystem:
        case "npm":
            return ("npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", pinned)
        case "pypi":
            return ("uv", "pip", "install", "--no-deps", pinned)
        case "deno":
            return ("deno", "cache", pinned)
        case "oci":
            return ("docker", "pull", pinned)
        case _:
            raise LaunchPlanError(f"no install step is defined for the {ecosystem!r} ecosystem")


def build_launch_plan(
    spec: LaunchSpec,
    *,
    connection_id: str,
    location: ExecutionLocation,
    policy: ManagedLaunchPolicy | None = None,
) -> LaunchPlan:
    """Turn a validated launch spec into an install-and-run plan.

    Only ``managed-worker`` plans are built here. A ``user-connector`` server
    runs under the user's own policy on their own machine, so the platform
    records the spec and sends it to the connector rather than deciding how it
    starts; a connector plan still gets the same no-shell shape.
    """
    resolved = policy or ManagedLaunchPolicy()
    if location is ExecutionLocation.REMOTE:
        raise LaunchPlanError("a remote connection has no process to launch")

    command = spec.command.strip()
    if _command_name(command) in SHELL_COMMANDS:
        raise LaunchPlanError(
            "run the server program directly rather than through a shell, so its arguments "
            "cannot be reinterpreted"
        )
    argv = spec.argv()
    encoded = len(shlex.join(argv).encode())
    if encoded > resolved.max_argv_bytes:
        raise LaunchPlanError("this launch command is longer than a worker will accept")
    if len(spec.env) > resolved.max_env_entries:
        raise LaunchPlanError("this launch command sets more environment variables than allowed")

    if location is ExecutionLocation.MANAGED_WORKER:
        if _command_name(command) not in resolved.allowed_commands:
            allowed = ", ".join(sorted(resolved.allowed_commands))
            raise LaunchPlanError(
                f"a managed worker will not start {command!r}. Allowed: {allowed}. "
                "Run this server on a user connector instead."
            )
        mounts = _check_mounts(spec.mounts, resolved)
    else:
        mounts = spec.mounts

    install = _install_for(spec, argv)
    digest = _plan_digest(argv, install, spec.env, mounts, resolved)
    return LaunchPlan(
        connection_id=connection_id,
        run_argv=argv,
        install=install,
        env=dict(spec.env),
        mounts=mounts,
        cwd=spec.cwd,
        network=resolved.network,
        plan_digest=digest,
    )


def _check_mounts(mounts: Sequence[Mount], policy: ManagedLaunchPolicy) -> tuple[Mount, ...]:
    """Refuse mounts a managed worker will not honour."""
    if len(mounts) > policy.max_mounts:
        raise LaunchPlanError(f"a managed worker accepts at most {policy.max_mounts} mounts")
    checked: list[Mount] = []
    for mount in mounts:
        if not mount.source.startswith("/"):
            raise LaunchPlanError(
                f"mount source {mount.source!r} must be an absolute path on the runner"
            )
        if ".." in mount.target.split("/"):
            raise LaunchPlanError("a mount target cannot escape the worker's mount root")
        if not mount.target.startswith(policy.mount_root.rstrip("/") + "/"):
            raise LaunchPlanError(
                f"a managed worker only mounts under {policy.mount_root}; "
                f"{mount.target!r} is outside it"
            )
        checked.append(mount)
    return tuple(checked)


def _install_for(spec: LaunchSpec, argv: tuple[str, ...]) -> InstallStep | None:
    """Build the install step an argv implies, or ``None`` for a plain program."""
    program = argv[0]
    ecosystem = _ecosystem(program)
    if ecosystem is None:
        return None
    if len(argv) < 2:
        raise LaunchPlanError(f"{program} needs a package to run")
    # Runners accept flags before the package (``npx -y pkg@1``), so the
    # package is the first argument that is not a flag. Taking ``argv[1]``
    # would read ``-y`` as a package name and refuse a correct configuration.
    reference = next((arg for arg in argv[1:] if not arg.startswith("-")), None)
    if reference is None:
        raise LaunchPlanError(f"{program} was given flags but no package to run")
    package, version = split_package_reference(reference)
    if version is None:
        # Deliberately a refusal rather than a resolution: resolving here would
        # mean the install step and the run step could disagree, and the whole
        # point of the split is that the version is decided once, by the user.
        raise LaunchPlanError(
            f"pin {package!r} to an exact version. A server that installs "
            f"{package!r} unpinned runs whatever the registry publishes next, in a "
            "worker that holds your credential.",
            kind="unpinned_package",
        )
    return InstallStep(
        argv=_install_argv(ecosystem, package, version),
        ecosystem=ecosystem,
        package=package,
        version=version,
    )


def _plan_digest(
    argv: tuple[str, ...],
    install: InstallStep | None,
    env: Mapping[str, str],
    mounts: Sequence[Mount],
    policy: ManagedLaunchPolicy,
) -> str:
    """A stable digest over everything that changes what would run.

    Secret *values* are excluded, and their names are not: the digest is what
    decides whether an installed tree may be reused, and a rotated credential
    must not force a reinstall while a changed variable name must.
    """
    payload = json.dumps(
        {
            "argv": list(argv),
            "install": install.as_dict() if install else None,
            "env": sorted(env),
            "mounts": [[mount.source, mount.target, mount.read_only] for mount in mounts],
            "network": policy.network,
            "install_root": policy.install_root,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def resolve_env(
    env: Mapping[str, str],
    secrets: Mapping[str, str],
) -> dict[str, str]:
    """Resolve ``secret://NAME`` references into the worker's environment.

    Only the names this connection declared are resolvable. A spec that
    references a secret it did not declare is refused rather than left empty,
    because an empty credential produces an authentication failure that reads
    like the user's mistake.
    """
    resolved: dict[str, str] = {}
    for name, value in env.items():
        if value.startswith("secret://"):
            key = value[len("secret://") :]
            if key not in secrets:
                raise LaunchPlanError(
                    f"this server needs a value for {key!r} that the connection does not have"
                )
            resolved[name] = secrets[key]
        elif "${" in value:
            raise LaunchPlanError(
                f"{name} still contains an unresolved reference; provide its value when saving"
            )
        else:
            resolved[name] = value
    return resolved


def declared_secret_names(env: Mapping[str, str], headers: Mapping[str, str]) -> list[str]:
    """Every secret name a connection's spec refers to, sorted.

    Used to decide what the dialog must ask for, and to check that a stored
    credential set has not lost one since the connection was saved.
    """
    prefix = "secret://"
    names = {value[len(prefix) :] for value in env.values() if value.startswith(prefix)}
    names.update(value[len(prefix) :] for value in headers.values() if value.startswith(prefix))
    return sorted(names)


__all__ = [
    "MANAGED_ALLOWED_COMMANDS",
    "PACKAGE_RUNNERS",
    "SHELL_COMMANDS",
    "InstallStep",
    "LaunchPlan",
    "LaunchPlanError",
    "ManagedLaunchPolicy",
    "build_launch_plan",
    "declared_secret_names",
    "resolve_env",
    "split_package_reference",
]
