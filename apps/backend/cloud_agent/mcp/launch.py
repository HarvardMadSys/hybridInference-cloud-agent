"""Deciding whether a stdio server may start, reproducibly and without a shell.

A stdio server runs in the task's sandbox, next to the agent, behind the
sandbox's MCP gateway (cloud_agent_shared.sandbox_mcp). Two rules make what
runs there the program its owner saw.

**Nothing resolves at run time that could change under the owner.** ``npx
some-server`` downloads whatever ``latest`` means today and produces a
different program every week. So a launch names one exact release of one
package, and a version change is a connection edit.

**No shell, anywhere.** ``argv`` is executed directly. That is not a hardening
measure on top of escaping; it is the removal of the thing that needs escaping.

The module is pure: it decides what would run, and refuses what it cannot
express safely. The gateway starts it.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from cloud_agent.mcp.models import ExecutionLocation, LaunchSpec
from cloud_agent_shared.sandbox_mcp import SANDBOX_MCP_COMMANDS

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

#: Bounds on one launch, whatever it starts.
MAX_ENV_ENTRIES = 64
MAX_ARGV_BYTES = 8192

#: A package reference: ``name``, ``@scope/name``, ``name@1.2.3``,
#: ``name==1.2.3``, ``name>=1,<2``. Compared as text; the installer decides
#: what it means, and this only decides whether a version is present at all.
_PINNED_NPM = re.compile(r"^(?P<name>(?:@[^/@]+/)?[^@/][^@]*)@(?P<version>[^@]+)$")
#: ``name[extra,...]`` is one package with optional parts: ``browser-use[cli]==0.7.10``.
_PINNED_PYPI = re.compile(
    r"^(?P<name>[A-Za-z0-9._-]+(?:\[[A-Za-z0-9._,-]+\])?)\s*(?:==|>=|~=)\s*(?P<version>.+)$"
)
#: An exact release: what a sandbox server must name, since the sandbox installs
#: it at run time. ``latest``, ``^1.2`` or ``>=1`` would change under the user.
_EXACT_VERSION = re.compile(
    r"^(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,3}(?:[-+.][0-9A-Za-z.-]+)?$"
)


def is_exact_release(version: str) -> bool:
    """Whether ``version`` names one release, as a sandbox server's package must."""
    return _EXACT_VERSION.match(version) is not None


class LaunchPlanError(ValueError):
    """Raised when a launch spec cannot be expressed as a plan."""

    def __init__(self, message: str, *, kind: str = "launch_failed") -> None:
        """Record the diagnosis kind alongside the message."""
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class InstallStep:
    """The one command that fixes what will run, and the version it fixes."""

    argv: tuple[str, ...]
    ecosystem: str
    package: str
    version: str

    def as_dict(self) -> dict[str, object]:
        """Render for the plan."""
        return {
            "argv": list(self.argv),
            "ecosystem": self.ecosystem,
            "package": self.package,
            "version": self.version,
        }


@dataclass(frozen=True)
class LaunchPlan:
    """What the sandbox's gateway is told: this pinned package, run as this argv."""

    connection_id: str
    run_argv: tuple[str, ...]
    install: InstallStep
    env: Mapping[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Render the plan, with no secret values in it."""
        return {
            "connection_id": self.connection_id,
            "run_argv": list(self.run_argv),
            "install": self.install.as_dict(),
            "env_names": sorted(self.env),
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
) -> LaunchPlan:
    """Turn a validated launch spec into the plan the sandbox's gateway runs.

    A stdio server runs only in the sandbox, so it is checked against the rules
    that gateway runs by (:func:`_check_sandbox`).
    """
    if location is ExecutionLocation.REMOTE:
        raise LaunchPlanError("a remote connection has no process to launch")

    command = spec.command.strip()
    if _command_name(command) in SHELL_COMMANDS:
        raise LaunchPlanError(
            "run the server program directly rather than through a shell, so its arguments "
            "cannot be reinterpreted"
        )
    argv = spec.argv()
    if len(shlex.join(argv).encode()) > MAX_ARGV_BYTES:
        raise LaunchPlanError("this launch command is longer than the sandbox will accept")
    if len(spec.env) > MAX_ENV_ENTRIES:
        raise LaunchPlanError("this launch command sets more environment variables than allowed")
    install = _check_sandbox(spec, argv)
    return LaunchPlan(
        connection_id=connection_id, run_argv=argv, install=install, env=dict(spec.env)
    )


def _check_sandbox(spec: LaunchSpec, argv: tuple[str, ...]) -> InstallStep:
    """Refuse what a sandbox server may not be (cloud_agent_shared.sandbox_mcp).

    It runs next to repository code, behind the sandbox's MCP gateway, which
    starts only a package runner with one exactly pinned package. So: ``npx``
    or ``uvx`` by name, an exact version, and no mounts or working directory (it
    already has the workspace). It may use credentials, as the agent's own
    commands use the owner's environment: ``secret://NAME`` values sealed with
    the connection, and ``${NAME}`` filled from the chat's environment.
    """
    if spec.command not in SANDBOX_MCP_COMMANDS:
        raise LaunchPlanError(
            f"a sandbox server starts with npx or uvx and one pinned package, not {spec.command!r}"
        )
    if spec.mounts or spec.cwd:
        raise LaunchPlanError("a sandbox server runs in the workspace; it takes no mounts or cwd")
    install = _install_for(spec, argv)
    if install is None:  # pragma: no cover - npx and uvx are package runners
        raise LaunchPlanError("a sandbox server needs a package to run")
    if not is_exact_release(install.version):
        raise LaunchPlanError(
            f"pin {install.package!r} to an exact release (for example "
            f"{install.package}@1.2.3), not {install.version!r}",
            kind="unpinned_package",
        )
    reference = next(arg for arg in argv[1:] if not arg.startswith("-"))
    if install.ecosystem == "pypi" and "==" not in reference:
        raise LaunchPlanError(
            f"pin {install.package!r} with == to an exact release", kind="unpinned_package"
        )
    return install


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
        # point of the split is that the version is decided once, when it is
        # saved. An import pins a bare name before it gets here (package_pins).
        raise LaunchPlanError(
            f"pin {package!r} to an exact version. A server that installs "
            f"{package!r} unpinned runs whatever the registry publishes next, in a "
            "sandbox that holds your credentials.",
            kind="unpinned_package",
        )
    return InstallStep(
        argv=_install_argv(ecosystem, package, version),
        ecosystem=ecosystem,
        package=package,
        version=version,
    )


def declared_secret_names(env: Mapping[str, str], headers: Mapping[str, str]) -> list[str]:
    """Every secret name a connection's spec refers to, sorted.

    Used to decide what the dialog must ask for, and to check that a stored
    credential set has not lost one since the connection was saved.
    """
    prefix = "secret://"
    names = {value[len(prefix) :] for value in env.values() if value.startswith(prefix)}
    names.update(value[len(prefix) :] for value in headers.values() if value.startswith(prefix))
    return sorted(names)


def _program(spec: Mapping[str, Any]) -> tuple[str, ...]:
    """What an edit must leave alone for a program to keep its keys.

    The whole argv, except the release of a package runner's package: a new
    release of the same package is the same program. Every other argument
    stays part of it, because a runner's options and the arguments after the
    package can change what runs (``npx --package pkg@1 --call 'node x.js'``
    installs ``pkg`` but runs ``x.js``).
    """
    command = str(spec.get("command") or "")
    args = tuple(str(arg) for arg in spec.get("args") or ())
    if _ecosystem(command) is not None:
        # The package is found as _install_for finds it.
        index = next((i for i, arg in enumerate(args) if not arg.startswith("-")), None)
        if index is not None:
            try:
                package = split_package_reference(args[index])[0]
            except LaunchPlanError:
                pass
            else:
                return (command, *args[:index], package, *args[index + 1 :])
    return (command, *args)


def edited_secret_env(
    row: Mapping[str, Any], rendered: Mapping[str, Any] | None, keys: Mapping[str, str]
) -> tuple[dict[str, Any], dict[str, str]]:
    """The launch an edit stores, with the keys it keeps, and the keys the edit sets.

    A program's keys are its ``secret://NAME`` values, stored sealed under
    ``env_secrets`` in the connection's ``launch_spec``. ``rendered`` is the
    submitted launch as validated, or ``None`` when the edit changes only keys
    and the stored launch stays. Either way no sealed value comes from the
    request: each name the launch references keeps its stored ciphertext
    unless ``keys`` sets it, and a blank value sets nothing, as every
    write-only editor treats one. A name the launch no longer references is
    removed with the reference. The keys set are returned for the caller to
    judge and seal; a stored key is not judged again. Stored keys stay only
    with the same program (:func:`_program`): an edit that runs another one
    is refused until it sends them again.
    """
    stored = row.get("launch_spec")
    stored = stored if isinstance(stored, Mapping) else {}
    if rendered is None and not stored.get("command"):
        raise LaunchPlanError("a stdio connection needs a command")
    source = stored if rendered is None else rendered
    edited = {key: value for key, value in source.items() if key != "env_secrets"}
    env = {str(key): str(value) for key, value in (edited.get("env") or {}).items()}
    wanted = declared_secret_names(env, {})
    sealed = stored.get("env_secrets")
    sealed = sealed if isinstance(sealed, Mapping) else {}
    setting = {name: keys[name] for name in wanted if keys.get(name)}
    kept = {name: str(sealed[name]) for name in wanted if name in sealed and name not in setting}
    if kept and rendered is not None and _program(rendered) != _program(stored):
        # A key was given to one program; another one (a different package or
        # command) could send it anywhere the sandbox reaches. As a remote
        # connection's credential is dropped with its address, it is not
        # passed on, and the edit says so instead of storing a launch whose
        # key the claim would later refuse.
        raise LaunchPlanError(
            "this edit runs a different program, so it is not given the stored keys; "
            "send them again: " + ", ".join(sorted(kept))
        )
    if kept:
        edited["env_secrets"] = kept
    return edited, setting


__all__ = [
    "MAX_ARGV_BYTES",
    "MAX_ENV_ENTRIES",
    "PACKAGE_RUNNERS",
    "SHELL_COMMANDS",
    "InstallStep",
    "LaunchPlan",
    "LaunchPlanError",
    "build_launch_plan",
    "declared_secret_names",
    "edited_secret_env",
    "is_exact_release",
    "split_package_reference",
]
