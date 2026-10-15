"""SSH servers an owner registered, as one sandbox group is allowed to reach them.

The control plane sends a group's whole SSH state on every change and at every
turn: which public ``IPv4:port`` pairs its bridge may reach, and which servers
each resident member may log in to with which key. The host remembers the
newest state it was sent, so a late delivery cannot reopen what a newer one
closed, and it writes keys only for the member whose turn a state starts, so
every key in a member's sandbox is one its running turn masks. See
``docs/agents/specs/2026-10-15-user-ssh-servers-design.zh-CN.md``.

Every value here ends up in an nftables element, an OpenSSH configuration file
or a ``known_hosts`` line, so each is validated to a shape that cannot carry a
second directive, a newline or a path.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import struct

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cloud_agent_shared.egress import PROTECTED_IPV4

SSH_ACCESS_CAPABILITY = "ssh-access-v1"

#: What an agent types after ``ssh``. No dot, so it never shadows a real host
#: name such as ``github.com``.
SERVER_NAME = re.compile(r"[a-z][a-z0-9-]{1,31}")
#: A login name OpenSSH passes through unchanged: no whitespace, ``%`` token,
#: quote or leading dash.
USERNAME = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._-]{0,31}")
HOST_KEY_TYPES = (
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "ssh-rsa",
)
#: The signature algorithms OpenSSH accepts for each pinned host key type.
#: Plain ``ssh-rsa`` (SHA-1) signatures are off by default in current OpenSSH,
#: so an RSA host key is verified with its SHA-2 signatures.
HOST_KEY_ALGORITHMS = {
    "ssh-ed25519": "ssh-ed25519",
    "ecdsa-sha2-nistp256": "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384": "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521": "ecdsa-sha2-nistp521",
    "ssh-rsa": "rsa-sha2-512,rsa-sha2-256",
}
#: The highest member slot a retained sandbox creates a user for.
MAX_SLOT_ID = 127
_PRIVATE_KEY = re.compile(
    r"-----BEGIN OPENSSH PRIVATE KEY-----\n[A-Za-z0-9+/=\n]+-----END OPENSSH PRIVATE KEY-----\n"
)
_MAX_PRIVATE_KEY_CHARS = 16384
_MAX_HOST_KEY_CHARS = 8192
#: Protocol bounds, not product limits: far beyond any real selection, they
#: only keep one request from being unbounded.
_MAX_SERVERS_PER_MEMBER = 1024
_MAX_MEMBERS = MAX_SLOT_ID + 1
_PROTECTED = tuple(ipaddress.IPv4Network(block) for block in PROTECTED_IPV4)


class SshAccessError(ValueError):
    """A value that cannot be used for SSH access safely."""


def require_server_name(value: str) -> str:
    """Validate the alias an agent connects with."""
    if not SERVER_NAME.fullmatch(value):
        raise SshAccessError(
            "Server names are 2-32 characters: a lowercase letter, then lowercase "
            "letters, digits or hyphens"
        )
    return value


def require_username(value: str) -> str:
    """Validate a login name that is written into an SSH configuration."""
    if not USERNAME.fullmatch(value):
        raise SshAccessError(
            "Usernames are 1-32 letters, digits, '.', '_' or '-', not starting with '-'"
        )
    return value


def require_public_ipv4(value: str, extra_protected: tuple[ipaddress.IPv4Network, ...] = ()) -> str:
    """Return ``value`` as a canonical public IPv4 address, or refuse it."""
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise SshAccessError("Server addresses must be IPv4 addresses") from exc
    if any(address in block for block in (*_PROTECTED, *extra_protected)):
        raise SshAccessError("Only public IPv4 addresses can be reached from a sandbox")
    return str(address)


def require_host_key(key_type: str, key: str) -> tuple[str, str]:
    """Validate a host public key whose blob names the same algorithm."""
    if key_type not in HOST_KEY_TYPES:
        raise SshAccessError(f"Unsupported host key type {key_type!r}")
    if not key or len(key) > _MAX_HOST_KEY_CHARS:
        raise SshAccessError("Host key is empty or too long")
    try:
        blob = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SshAccessError("Host key is not base64") from exc
    if len(blob) < 4:
        raise SshAccessError("Host key is truncated")
    (length,) = struct.unpack(">I", blob[:4])
    if blob[4 : 4 + length] != key_type.encode():
        raise SshAccessError("Host key does not match its type")
    return key_type, key


def host_key_fingerprint(key: str) -> str:
    """The ``SHA256:`` fingerprint ``ssh-keygen -l`` prints for a public key blob."""
    digest = hashlib.sha256(base64.b64decode(key)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


class SshServerAccess(BaseModel):
    """One server one member may log in to. Never log it: it carries a key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str = Field(min_length=1, max_length=64, pattern=r"^[0-9a-f-]+$")
    name: str
    address: str
    port: int = Field(ge=1, le=65535)
    username: str
    host_key_type: str
    host_key: str
    private_key: str = Field(repr=False, max_length=_MAX_PRIVATE_KEY_CHARS)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return require_server_name(value)

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return require_public_ipv4(value)

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        return require_username(value)

    @field_validator("private_key")
    @classmethod
    def _private_key(cls, value: str) -> str:
        if not _PRIVATE_KEY.fullmatch(value):
            raise SshAccessError("Private key must be one OpenSSH private key")
        return value

    @model_validator(mode="after")
    def _host_key(self) -> SshServerAccess:
        require_host_key(self.host_key_type, self.host_key)
        return self

    def digest(self) -> str:
        """Names this exact entry, key included, without revealing the key.

        Two states agree on a server only if every field matches: a new key, a
        new name or a new host key is a different entry.
        """
        canonical = json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class SshMemberAccess(BaseModel):
    """The servers one chat's member may use; ``slot_id`` once it has one."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_id: str = Field(min_length=1, max_length=128)
    slot_id: int | None = Field(default=None, ge=0, le=MAX_SLOT_ID)
    servers: list[SshServerAccess] = Field(
        default_factory=list, repr=False, max_length=_MAX_SERVERS_PER_MEMBER
    )

    @model_validator(mode="after")
    def _unique_names(self) -> SshMemberAccess:
        names = [server.name for server in self.servers]
        if len(names) != len(set(names)):
            raise SshAccessError("A member's servers must have distinct names")
        return self


class SshAccess(BaseModel):
    """A sandbox group's whole SSH state at one revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    members: list[SshMemberAccess] = Field(
        default_factory=list, repr=False, max_length=_MAX_MEMBERS
    )

    @model_validator(mode="after")
    def _unique_members(self) -> SshAccess:
        threads = [member.thread_id for member in self.members]
        slots = [member.slot_id for member in self.members if member.slot_id is not None]
        if len(threads) != len(set(threads)) or len(slots) != len(set(slots)):
            raise SshAccessError("Each chat and each slot may appear once")
        return self

    def targets(self) -> set[tuple[str, int]]:
        """Every ``(address, port)`` the group's bridge may reach over SSH."""
        return {
            (server.address, server.port) for member in self.members for server in member.servers
        }

    def resident_members(self) -> list[SshMemberAccess]:
        """The members that have a slot, and so a home to write files into."""
        return [member for member in self.members if member.slot_id is not None]

    def with_slot(self, thread_id: str, slot_id: int) -> SshAccess:
        """This state with ``thread_id``'s member placed in ``slot_id``.

        A claim is computed before the turn reserves its member slot; the
        runner fills it in. Another member listed in that slot is a member the
        claim saw before this one took the slot over, so it is dropped.
        """
        members = [
            member.model_copy(update={"slot_id": slot_id})
            if member.thread_id == thread_id
            else member
            for member in self.members
            if member.thread_id == thread_id or member.slot_id != slot_id
        ]
        return self.model_copy(update={"members": members})

    def secrets(self) -> dict[str, str]:
        """Private keys by name, for the output redactor."""
        return {
            f"ssh-key:{server.name}": server.private_key
            for member in self.members
            for server in member.servers
        }


__all__ = [
    "HOST_KEY_ALGORITHMS",
    "HOST_KEY_TYPES",
    "MAX_SLOT_ID",
    "SERVER_NAME",
    "SSH_ACCESS_CAPABILITY",
    "USERNAME",
    "SshAccess",
    "SshAccessError",
    "SshMemberAccess",
    "SshServerAccess",
    "host_key_fingerprint",
    "require_host_key",
    "require_public_ipv4",
    "require_server_name",
    "require_username",
]
