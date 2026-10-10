"""Runtime settings an admin sets from the dashboard rather than a host's ``.env``.

Each setting here used to be read only from the host's environment, so changing
one meant editing ``.env`` on every machine and redeploying. The admin pages
now set them: the Settings page once for every server, and a server's details
on the Servers page for that server alone. Each host picks the values up from
its next heartbeat answer.

``label`` and ``description`` are what those pages show, so a description says
when a new value takes effect where that is not the next turn.

Precedence, highest first:

1. a value set for one server on the admin page;
2. the value set for all servers;
3. the built-in default.

The control plane sends the complete effective configuration on every heartbeat.
The host's old environment variables remain a bootstrap source only, before its
first settings answer arrives. They are reported as ``settings_local`` for
diagnostics; clearing an admin value never reactivates an old environment value.

Both images ship this module. The control plane validates an admin's change
against it, and the host resolves what it receives against it again, so a value
that reaches a host by any route is still a whole number inside these bounds.
The bounds are typo guards, not statements about what a machine can carry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: Which host process applies a setting. The runner reads its values when it
#: hands a claimed turn to the process that runs it; the workspace broker is a
#: separate container and is told its values by the runner.
SettingScope = Literal["runner", "broker", "both"]
SettingUnit = Literal["seconds", "MiB", "KiB", "count"]


@dataclass(frozen=True)
class HostSetting:
    """One runtime setting: where it lives on a host, and what an admin may set."""

    key: str
    env: str
    label: str
    description: str
    unit: SettingUnit
    default: int
    minimum: int
    maximum: int
    scope: SettingScope

    def accepts(self, value: object) -> bool:
        """Whether ``value`` is a whole number an admin may set for this setting."""
        return (
            isinstance(value, int)
            and not isinstance(value, bool)
            and self.minimum <= value <= self.maximum
        )


#: In the order the admin pages list them: the session's own limits, then the
#: sandbox's, then the terminals'.
HOST_SETTINGS: tuple[HostSetting, ...] = (
    HostSetting(
        key="agent_timeout_s",
        env="AGENT_TIMEOUT_S",
        label="Turn time limit",
        description=(
            "How long one agent turn may run before the host stops it. Applies to "
            "turns that start after the server picks the value up."
        ),
        unit="seconds",
        default=3600,
        minimum=60,
        maximum=86400,
        scope="runner",
    ),
    HostSetting(
        key="source_import_max_mib",
        env="AGENT_SOURCE_IMPORT_MAX_MIB",
        label="Repository size limit",
        description=(
            "Largest repository a session's first turn may import, counting the "
            "checkout's files and the fetched commit's Git objects. A larger "
            "repository fails before the agent starts."
        ),
        unit="MiB",
        default=1024,
        minimum=1,
        maximum=65536,
        scope="runner",
    ),
    HostSetting(
        key="chat_idle_timeout_s",
        env="AGENT_CHAT_IDLE_TIMEOUT_S",
        label="Sandbox idle timeout",
        description=(
            "How long a session's sandbox keeps running with no turn, file, Git "
            "or terminal activity before it is stopped. A stopped sandbox is kept "
            "and restarts on the next turn. A new value applies from each "
            "sandbox's next idle period."
        ),
        unit="seconds",
        default=1800,
        minimum=60,
        maximum=604800,
        scope="both",
    ),
    HostSetting(
        key="shared_resident_capacity",
        env="AGENT_SHARED_RESIDENT_CAPACITY",
        label="Sessions per sandbox",
        description=(
            "How many sessions — a conversation and its forks — one new sandbox "
            "can hold; Fork is refused once it is full. Sandboxes that already "
            "exist keep the number they were created with."
        ),
        unit="count",
        default=8,
        minimum=2,
        maximum=128,
        scope="runner",
    ),
    HostSetting(
        key="terminal_idle_ttl_s",
        env="AGENT_TERMINAL_IDLE_TTL_SECONDS",
        label="Terminal idle timeout",
        description=(
            "How long an interactive terminal may sit with no input or output "
            "before it is closed. Applies to open terminals too."
        ),
        unit="seconds",
        default=1800,
        minimum=60,
        maximum=86400,
        scope="broker",
    ),
    HostSetting(
        key="terminal_hard_ttl_s",
        env="AGENT_TERMINAL_HARD_TTL_SECONDS",
        label="Terminal max lifetime",
        description=(
            "The longest one interactive terminal may stay open, even while it is "
            "in use. Applies to open terminals too: a shorter value closes those "
            "already past it."
        ),
        unit="seconds",
        default=28800,
        minimum=60,
        # A day, not the week the idle stop allows: this bounds how long a
        # terminal's credentials stay exposed, so the page cannot stretch it
        # past what a working day needs.
        maximum=86400,
        scope="broker",
    ),
    HostSetting(
        key="terminal_max_sessions",
        env="AGENT_TERMINAL_MAX_SESSIONS",
        label="Terminals per server",
        description=(
            "How many interactive terminals one server may hold open at once, "
            "across all sessions. A lower value refuses new terminals and closes "
            "none."
        ),
        unit="count",
        default=64,
        minimum=1,
        maximum=4096,
        scope="broker",
    ),
    HostSetting(
        key="terminal_max_sessions_per_workspace",
        env="AGENT_TERMINAL_MAX_SESSIONS_PER_WORKSPACE",
        label="Terminals per workspace",
        description="Maximum open terminals in one shared workspace. A lower value refuses new terminals and closes none.",
        unit="count",
        default=4,
        minimum=1,
        maximum=4,
        scope="broker",
    ),
    HostSetting(
        key="workspace_command_timeout_s",
        env="AGENT_WORKSPACE_COMMAND_TIMEOUT_S",
        label="Workspace command timeout",
        description="Default time limit for commands and Git inspection in the workspace. Applies to new requests.",
        unit="seconds",
        default=60,
        minimum=1,
        maximum=120,
        scope="broker",
    ),
    HostSetting(
        key="workspace_file_write_max_mib",
        env="AGENT_WORKSPACE_FILE_WRITE_MAX_MIB",
        label="File write size limit",
        description="Largest UTF-8 file replacement accepted by the workspace editor. Applies to new requests.",
        unit="MiB",
        default=2,
        minimum=1,
        maximum=2,
        scope="broker",
    ),
    HostSetting(
        key="workspace_file_preview_max_kib",
        env="AGENT_WORKSPACE_FILE_PREVIEW_MAX_KIB",
        label="File preview size limit",
        description="Largest file preview returned by the workspace browser. Applies to new requests.",
        unit="KiB",
        default=512,
        minimum=1,
        maximum=512,
        scope="broker",
    ),
    HostSetting(
        key="workspace_command_output_max_mib",
        env="AGENT_WORKSPACE_COMMAND_OUTPUT_MAX_MIB",
        label="Command output size limit",
        description="Maximum output from a workspace command or Git inspection. Larger command output stops the command.",
        unit="MiB",
        default=2,
        minimum=1,
        maximum=2,
        scope="broker",
    ),
    HostSetting(
        key="workspace_directory_max_entries",
        env="AGENT_WORKSPACE_DIRECTORY_MAX_ENTRIES",
        label="Directory listing limit",
        description="Maximum entries returned for one directory in the workspace browser. Applies to new requests.",
        unit="count",
        default=5000,
        minimum=1,
        maximum=5000,
        scope="broker",
    ),
    HostSetting(
        key="terminal_buffer_max_mib",
        env="AGENT_TERMINAL_BUFFER_MAX_MIB",
        label="Terminal output buffer size",
        description="Output history retained for reconnecting terminal viewers. Applies to open terminals too.",
        unit="MiB",
        default=2,
        minimum=1,
        maximum=2,
        scope="broker",
    ),
    HostSetting(
        key="terminal_buffer_max_events",
        env="AGENT_TERMINAL_BUFFER_MAX_EVENTS",
        label="Terminal output buffer events",
        description="Maximum events retained in a terminal's output history. Applies to open terminals too.",
        unit="count",
        default=4096,
        minimum=2,
        maximum=4096,
        scope="broker",
    ),
    HostSetting(
        key="terminal_output_max_mib",
        env="AGENT_TERMINAL_OUTPUT_MAX_MIB",
        label="Terminal total output limit",
        description="Total output allowed during one terminal's lifetime. An open terminal exceeding a lower limit stops on its next output.",
        unit="MiB",
        default=32,
        minimum=1,
        maximum=32,
        scope="broker",
    ),
    HostSetting(
        key="terminal_input_max_kib",
        env="AGENT_TERMINAL_INPUT_MAX_KIB",
        label="Terminal input size limit",
        description="Maximum input accepted in one terminal request, including pasted text. Applies to new requests.",
        unit="KiB",
        default=64,
        minimum=1,
        maximum=64,
        scope="broker",
    ),
    HostSetting(
        key="terminal_max_streams_per_session",
        env="AGENT_TERMINAL_MAX_STREAMS_PER_SESSION",
        label="Viewers per terminal",
        description="Maximum concurrent streams viewing one terminal. A lower value refuses new viewers and disconnects none.",
        unit="count",
        default=4,
        minimum=1,
        maximum=4,
        scope="broker",
    ),
)

HOST_SETTINGS_BY_KEY: dict[str, HostSetting] = {setting.key: setting for setting in HOST_SETTINGS}

#: The keys the runner applies itself, and those it hands to the broker.
#: ``chat_idle_timeout_s`` is in both: the runner reports it to the control
#: plane when a turn settles, and the broker arms its own idle sweep with it.
RUNNER_SETTING_KEYS = frozenset(
    setting.key for setting in HOST_SETTINGS if setting.scope in ("runner", "both")
)
BROKER_SETTING_KEYS = frozenset(
    setting.key for setting in HOST_SETTINGS if setting.scope in ("broker", "both")
)

#: The most keys a stored or reported settings map may carry.
MAX_SETTINGS_KEYS = 32
#: A reported value larger than this is not one any setting here could hold.
_MAX_REPORTED_VALUE = 1e12


def validate_admin_change(values: Mapping[str, Any]) -> dict[str, int | None]:
    """Check an admin's change: known settings, whole numbers in bounds, or ``None``.

    ``None`` clears the value, so the setting falls back to the next level of
    precedence. Raises ``ValueError`` naming the first problem.
    """
    if len(values) > MAX_SETTINGS_KEYS:
        raise ValueError(f"at most {MAX_SETTINGS_KEYS} settings can be changed at once")
    checked: dict[str, int | None] = {}
    for key, value in values.items():
        setting = HOST_SETTINGS_BY_KEY.get(key)
        if setting is None:
            raise ValueError(f"unknown setting {key!r}")
        if value is not None and not setting.accepts(value):
            raise ValueError(
                f"{setting.label} ({key}) must be a whole number from "
                f"{setting.minimum} through {setting.maximum} {setting.unit}"
            )
        checked[key] = value
    return checked


def admin_values(raw: Any, keys: Iterable[str] | None = None) -> dict[str, int]:
    """Read a stored or received map of admin values, keeping only usable ones.

    Tolerant on purpose. A value that was valid when it was saved can stop being
    one when a release narrows a bound or retires a setting, and a host can be
    sent a key it does not know yet. Dropping such an entry falls back to the
    next level of precedence, which is what a value nobody can apply should do.
    """
    if not isinstance(raw, dict):
        return {}
    wanted = None if keys is None else frozenset(keys)
    kept: dict[str, int] = {}
    for key, value in raw.items():
        setting = HOST_SETTINGS_BY_KEY.get(key) if isinstance(key, str) else None
        if setting is None or (wanted is not None and key not in wanted):
            continue
        if setting.accepts(value):
            kept[key] = value
    return kept


def default_values(keys: Iterable[str] | None = None) -> dict[str, int]:
    """Built-in values, optionally restricted to one process's settings."""
    wanted = None if keys is None else frozenset(keys)
    return {
        setting.key: setting.default
        for setting in HOST_SETTINGS
        if wanted is None or setting.key in wanted
    }


def merge_admin_values(global_values: Any, server_values: Any) -> dict[str, int]:
    """Complete values one server applies: overrides over globals over defaults."""
    return {**default_values(), **admin_values(global_values), **admin_values(server_values)}


def resolve(admin: Mapping[str, int], local: Mapping[str, float]) -> dict[str, float]:
    """Each local setting's effective value: an admin value when one is set.

    ``local`` is the host's own value for every setting it applies — its
    environment variable or the built-in default — and decides which keys are
    returned. ``admin`` is expected to come from :func:`admin_values`.
    """
    return {key: admin.get(key, value) for key, value in local.items()}


def reported_values(raw: Any) -> dict[str, int | float] | None:
    """A host's report of the values it uses, reduced to known, finite numbers.

    ``None`` means the host sent no report at all, which is different from an
    empty one. Integral floats become ints so a value an admin set reads back
    as the same number.
    """
    if not isinstance(raw, dict):
        return None
    kept: dict[str, int | float] = {}
    for key, value in raw.items():
        if key not in HOST_SETTINGS_BY_KEY:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value) or not 0 <= value <= _MAX_REPORTED_VALUE:
            continue
        kept[key] = int(value) if float(value).is_integer() else float(value)
    return kept


__all__ = [
    "BROKER_SETTING_KEYS",
    "HOST_SETTINGS",
    "HOST_SETTINGS_BY_KEY",
    "MAX_SETTINGS_KEYS",
    "RUNNER_SETTING_KEYS",
    "HostSetting",
    "admin_values",
    "default_values",
    "merge_admin_values",
    "reported_values",
    "resolve",
    "validate_admin_change",
]
