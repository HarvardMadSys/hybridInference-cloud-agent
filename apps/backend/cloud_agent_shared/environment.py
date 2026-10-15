"""Versioned user environment contracts shared by control plane and hosts."""

from __future__ import annotations

import io
import re
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Literal

from dotenv.parser import parse_stream
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_agent_shared.environment_snapshots import ENVIRONMENT_SNAPSHOT_CAPABILITY
from cloud_agent_shared.secret_redaction import MIN_SECRET_CHARS

if TYPE_CHECKING:
    from collections.abc import Iterable

ENVIRONMENT_CAPABILITY = "environment-v1"
MAX_VARIABLES = 100
MAX_EFFECTIVE_VARIABLES = 150
MAX_VALUE_BYTES = 32768
MAX_ENVIRONMENT_BYTES = 131072
MAX_SCRIPT_CHARS = 8000
MAX_IMPORT_BYTES = 1048576
ENVIRONMENT_DOMAIN = b"hybridinference/environment-variable/v1"
DOCKER_CLIENT_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "GODEBUG",
        "GOGC",
        "GOMAXPROCS",
        "GOMEMLIMIT",
        "GOTRACEBACK",
        "GLIBC_TUNABLES",
    }
)
DOCKER_CLIENT_PREFIXES = ("DOCKER_", "BUILDKIT_", "BUILDX_", "COMPOSE_", "LD_", "OTEL_", "XDG_")
PROXY_ENV = frozenset({"http_proxy", "https_proxy", "no_proxy", "all_proxy", "ftp_proxy"})
RESERVED_NAMES = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "MCP_TOOL_TIMEOUT",
        "DISABLE_TELEMETRY",
        "DISABLE_AUTOUPDATER",
        "DISABLE_ERROR_REPORTING",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "GH_HOST",
        "GH_REPO",
        "GH_PROMPT_DISABLED",
        "GIT_TERMINAL_PROMPT",
        "GIT_CONFIG_NOSYSTEM",
        "HOME",
        "PATH",
        "USER",
        "LOGNAME",
        "SHELL",
        "PWD",
        "TERM",
        "BASH_ENV",
        "ENV",
        "WORKSPACE_COMMAND",
        "WORKSPACE_CWD",
    }
)
RESERVED_PREFIXES = (
    "ANTHROPIC_",
    "CLAUDE_",
    "CODEX_",
    "OPENCODE_",
    "KILO_",
    "PI_",
    "GIT_CONFIG_",
    "AGENT_",
    "FREEINFERENCE_",
    *DOCKER_CLIENT_PREFIXES,
)
Phase = Literal["setup", "runtime", "both"]


class EnvironmentInputError(ValueError):
    """A safe environment validation message containing no supplied value."""


def validate_secret(name: str, value: str, secret: bool) -> None:
    """Refuse ambiguous short credentials instead of silently disabling their protection."""
    if secret and value and len(value.strip()) < MIN_SECRET_CHARS:
        raise EnvironmentInputError(
            f"Secret {name} must contain at least {MIN_SECRET_CHARS} characters; "
            "use Ordinary only for a non-sensitive value"
        )


def validate_name(name: str) -> str:
    """Refuse names that affect platform credentials or host executables."""
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
        raise EnvironmentInputError("Invalid variable name")
    if name in RESERVED_NAMES | DOCKER_CLIENT_ENV or name.startswith(RESERVED_PREFIXES):
        raise EnvironmentInputError(f"Variable {name} is reserved by the platform")
    if name.lower() in PROXY_ENV:
        raise EnvironmentInputError(f"Variable {name} is reserved by the platform")
    return name


class EnvironmentVariable(BaseModel):
    """A value-bearing variable, never suitable for request logging."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    value: str = Field(repr=False)
    secret: bool = True
    phase: Phase = "both"
    source: Literal["global", "environment", "override"] | None = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        """Validate a user-controlled process environment name."""
        return validate_name(value)

    @field_validator("value")
    @classmethod
    def valid_value(cls, value: str) -> str:
        """Bound process environment strings without including rejected values."""
        if "\0" in value or len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise EnvironmentInputError(
                "Variable values must be UTF-8 without NUL and at most 32 KiB"
            )
        return value


class VariableEdit(BaseModel):
    """An explicit keep, replace or remove operation on a saved variable."""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str
    value: str | None = Field(default=None, repr=False)
    secret: bool = True
    phase: Phase = "both"
    operation: Literal["keep", "replace", "remove"] = "replace"

    @model_validator(mode="after")
    def valid_edit(self) -> VariableEdit:
        """Validate edits without permitting a secret-to-plain keep operation."""
        validate_name(self.name)
        if self.operation == "replace":
            if self.value is None:
                raise EnvironmentInputError("Replacement variables require a value")
            EnvironmentVariable(
                name=self.name, value=self.value, secret=self.secret, phase=self.phase
            )
            validate_secret(self.name, self.value, self.secret)
        elif self.value is not None:
            raise EnvironmentInputError("Only replacement operations accept a value")
        return self


class EnvironmentAction(BaseModel):
    """A stable named command executed through the owner terminal surface."""

    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=80)
    command: str = Field(min_length=1, max_length=MAX_SCRIPT_CHARS)
    cwd: str = Field(default=".", max_length=4096)

    @field_validator("cwd")
    @classmethod
    def valid_cwd(cls, value: str) -> str:
        """Keep action paths relative to the shared workspace."""
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or "\0" in value:
            raise EnvironmentInputError("Action directory must be relative to the workspace")
        return str(path)

    @field_validator("command")
    @classmethod
    def valid_command(cls, value: str) -> str:
        """Reject process command strings with NUL."""
        if "\0" in value:
            raise EnvironmentInputError("Command cannot contain NUL")
        return value


class EnvironmentScripts(BaseModel):
    """The immutable non-secret portion of one environment version."""

    model_config = ConfigDict(extra="forbid", strict=True)
    setup_script: str = Field(default="", max_length=MAX_SCRIPT_CHARS)
    start_script: str = Field(default="", max_length=MAX_SCRIPT_CHARS)
    verify_script: str = Field(default="", max_length=MAX_SCRIPT_CHARS)
    #: Runs instead of the setup script when a workspace starts from an
    #: installation snapshot, for steps that follow the source (code generation,
    #: migrations). Never run otherwise.
    update_script: str = Field(default="", max_length=MAX_SCRIPT_CHARS)
    actions: list[EnvironmentAction] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_scripts(self) -> EnvironmentScripts:
        """Refuse duplicate operation identities and invalid shell text."""
        if len({a.id for a in self.actions}) != len(self.actions):
            raise EnvironmentInputError("Duplicate action id")
        scripts = (self.setup_script, self.start_script, self.verify_script, self.update_script)
        if any("\0" in v for v in scripts):
            raise EnvironmentInputError("Scripts cannot contain NUL")
        return self


class BoundEnvironment(EnvironmentScripts):
    """A frozen execution binding sent only over protected execution channels."""

    binding_id: str
    environment_id: str | None = None
    revision: int = 0
    global_revision: int = 0
    variables: list[EnvironmentVariable] = Field(default_factory=list, repr=False, max_length=150)

    runtime_revision: int = 0
    runtime_variables: list[EnvironmentVariable] | None = Field(
        default=None, repr=False, max_length=150
    )
    previous_secrets: dict[str, str] = Field(default_factory=dict, repr=False, max_length=512)
    skip_start: bool = False
    #: The control plane's HMAC of the setup script and setup-time variables.
    #: Equal fingerprints install the same thing, so a container marked done
    #: for one needs no installation for the other. Empty means unknown.
    setup_fingerprint: str = Field(default="", pattern=r"^(|[0-9a-f]{64})$")

    @model_validator(mode="after")
    def bound_variables(self) -> BoundEnvironment:
        """Validate the complete payload at the receiving host boundary."""
        validate_effective(self.variables)
        if sum(len(value.encode()) for value in self.previous_secrets.values()) > 262144:
            raise EnvironmentInputError("Runtime Secret history exceeds 256 KiB")
        if self.runtime_variables is not None:
            validate_effective(self.runtime_variables)
        return self

    def values(self, phase: Literal["setup", "runtime"]) -> dict[str, str]:
        """Select only variables available in this execution phase."""
        variables = (
            self.runtime_variables
            if phase == "runtime" and self.runtime_variables is not None
            else self.variables
        )
        return {v.name: v.value for v in variables if v.phase in (phase, "both")}

    def secrets(self) -> dict[str, str]:
        """Include all injected stages when constructing an output redactor."""
        result = {v.name: v.value for v in self.variables if v.secret and v.value}
        result.update(self.previous_secrets)
        for v in self.runtime_variables or []:
            if v.secret and v.value:
                result[v.name + "@runtime"] = v.value
        return result

    @property
    def required(self) -> bool:
        """Whether dropping this configuration would change execution.

        Runtime Secrets a refresh has since replaced count: earlier turns may
        have written them into the workspace, and only this binding still
        carries them for masking output and scanning snapshots. A binding has
        them only after it was required, as its stored flag stays.
        """
        return bool(
            self.variables
            or self.runtime_variables
            or self.previous_secrets
            or self.setup_script
            or self.start_script
            or self.actions
        )


#: Binding fields a host older than :data:`ENVIRONMENT_SNAPSHOT_CAPABILITY`
#: would refuse as unknown; they only matter to a host that builds or starts
#: from snapshots.
SNAPSHOT_BINDING_FIELDS = frozenset({"update_script", "setup_fingerprint"})


def environment_payload(
    environment: BoundEnvironment, capabilities: Iterable[str]
) -> dict[str, Any]:
    """The binding as JSON for one host, with snapshot fields only if it reads them."""
    if ENVIRONMENT_SNAPSHOT_CAPABILITY in capabilities:
        return environment.model_dump(mode="json")
    return environment.model_dump(mode="json", exclude=set(SNAPSHOT_BINDING_FIELDS))


def validate_effective(variables: list[EnvironmentVariable]) -> None:
    """Bound the effective union before either persistence or execution."""
    if len(variables) != len({v.name for v in variables}):
        raise EnvironmentInputError("Duplicate variable name")
    if len(variables) > MAX_EFFECTIVE_VARIABLES:
        raise EnvironmentInputError("Effective environment exceeds 150 variables")
    size = sum(len(v.name.encode()) + len(v.value.encode()) + 2 for v in variables)
    if size > MAX_ENVIRONMENT_BYTES:
        raise EnvironmentInputError("Effective environment exceeds 128 KiB")


def merge_variables(*scopes: list[EnvironmentVariable]) -> list[EnvironmentVariable]:
    """Override complete definitions before filtering execution stages."""
    sources = ("global", "environment", "override")
    merged = {
        v.name: v.model_copy(update={"source": sources[min(index, 2)]})
        for index, scope in enumerate(scopes)
        for v in scope
    }
    result = list(merged.values())
    for variable in result:
        validate_secret(variable.name, variable.value, variable.secret)
    validate_effective(result)
    return result


def parse_env(text: str, *, ordinary_names: list[str] | None = None) -> list[EnvironmentVariable]:
    """Parse dotenv syntax without interpolation or shell execution."""
    if len(text.encode("utf-8")) > MAX_IMPORT_BYTES:
        raise EnvironmentInputError("Import exceeds 1 MiB")
    result: list[EnvironmentVariable] = []
    names: set[str] = set()
    for binding in parse_stream(io.StringIO(text)):
        if binding.error or (binding.key is not None and binding.value is None):
            raise EnvironmentInputError(f"Invalid dotenv syntax at line {binding.original.line}")
        if binding.key is None:
            continue
        if binding.key in names:
            raise EnvironmentInputError(f"Duplicate variable {binding.key}")
        names.add(binding.key)
        result.append(
            EnvironmentVariable(
                name=binding.key,
                value=binding.value or "",
                secret=binding.key not in (ordinary_names or []),
            )
        )
    if set(ordinary_names or []) - names:
        raise EnvironmentInputError("Ordinary selections must name variables in this import")
    if len(result) > MAX_VARIABLES:
        raise EnvironmentInputError("An environment accepts at most 100 variables")
    validate_effective(result)
    return result


def environment_limits() -> dict[str, int | str | list[str]]:
    """Expose the same limits and reserved vocabulary used by execution."""
    return {
        "capability": ENVIRONMENT_CAPABILITY,
        "secret_min_chars": MIN_SECRET_CHARS,
        "variables": MAX_VARIABLES,
        "effective_variables": MAX_EFFECTIVE_VARIABLES,
        "value_bytes": MAX_VALUE_BYTES,
        "total_bytes": MAX_ENVIRONMENT_BYTES,
        "script_chars": MAX_SCRIPT_CHARS,
        "import_bytes": MAX_IMPORT_BYTES,
        "actions": 20,
        "setup_timeout_seconds": 900,
        "start_timeout_seconds": 120,
        "verify_timeout_seconds": 300,
        "test_timeout_seconds": 1800,
        "reserved_names": sorted(RESERVED_NAMES | DOCKER_CLIENT_ENV),
        "reserved_prefixes": list(RESERVED_PREFIXES),
        "proxy_names": sorted(PROXY_ENV),
    }
