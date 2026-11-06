"""Install Codex's system MCP allowlist inside the retained sandbox.

The host supplies the deployment relay address before starting an agent. The
guest root writes only system policy; member credentials stay in the private
launch environment. No host mount or additional container capability is needed.

The allowlist names two servers: the relay, as ``session``, and the sandbox's
own MCP gateway, as ``sandbox`` (cloud_agent_shared.sandbox_mcp). The gateway's
address is the same in every sandbox, so naming it never changes a policy a
sibling Codex member is running against; the gateway itself decides, per
member credential, what a request may reach.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from cloud_agent_shared.sandbox_mcp import SANDBOX_MCP_URL as SANDBOX_MCP_URL


def relay_endpoint(base_url: str) -> str:
    """Validate the trusted deployment URL and name its merged MCP endpoint."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 32 for char in base_url)
    ):
        raise ValueError("Codex MCP needs a valid deployment relay URL")
    return base_url.rstrip("/").removesuffix("/v1") + "/v1/agent/mcp"


def install_policy_argv(endpoint: str, python: str = "/usr/bin/python3") -> list[str]:
    """The guest-root command that installs the policy for ``endpoint``.

    The one place that knows what the script takes, so a caller cannot run it
    with a different set of arguments than the host does.
    """
    return [python, "-I", "-c", INSTALL_POLICY, endpoint, SANDBOX_MCP_URL]


# Executed as guest root by the trusted host, never through repository code.
# Both directories and an existing policy must be root-owned and unwritable by
# members. Atomic replacement also makes simultaneous member launches safe.
INSTALL_POLICY = r"""
import json
import os
import stat
import sys
import tempfile
import tomllib

POLICY_DIRECTORIES = ("/etc", "/etc/codex")
POLICY = "/etc/codex/requirements.toml"
PROCESSES = "/proc"
OWNER_UID = 0

for directory in POLICY_DIRECTORIES:
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != OWNER_UID or info.st_mode & 0o022:
        raise RuntimeError("Codex policy directory is not protected")
info = os.lstat(POLICY)
if not stat.S_ISREG(info.st_mode) or info.st_uid != OWNER_UID or info.st_mode & 0o022:
    raise RuntimeError("Codex system policy is not protected")
SANDBOX_URL = sys.argv[2]
policy = (
    "[mcp_servers.session]\nidentity = { url = " + json.dumps(sys.argv[1]) + " }\n\n"
    "[mcp_servers.sandbox]\nidentity = { url = " + json.dumps(SANDBOX_URL) + " }\n"
)
with open(POLICY, encoding="utf-8") as handle:
    existing = handle.read()
if existing == policy:
    sys.exit(0)


def identity_url(servers, name):
    entry = servers.get(name)
    identity = entry.get("identity") if isinstance(entry, dict) else None
    if (
        isinstance(entry, dict)
        and set(entry) == {"identity"}
        and isinstance(identity, dict)
        and set(identity) == {"url"}
        and isinstance(identity["url"], str)
    ):
        return identity["url"]
    return None


def platform_relay_policy(document):
    # What this script writes: `session` naming one relay URL, and since the
    # sandbox gateway, `sandbox` naming its fixed address. Returns the relay
    # URL, or None for any other shape.
    servers = document.get("mcp_servers")
    if set(document) != {"mcp_servers"} or not isinstance(servers, dict):
        return None
    if set(servers) not in ({"session"}, {"session", "sandbox"}):
        return None
    if "sandbox" in servers and identity_url(servers, "sandbox") != SANDBOX_URL:
        return None
    return identity_url(servers, "session")


def codex_running():
    # The launching member's own processes are reaped before this runs, so a
    # Codex process left in the guest belongs to a sibling member.
    for entry in os.listdir(PROCESSES):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(PROCESSES, entry, "comm"), "rb") as handle:
                names = {handle.read().strip()}
            with open(os.path.join(PROCESSES, entry, "cmdline"), "rb") as handle:
                names.update(os.path.basename(arg) for arg in handle.read().split(b"\0")[:2])
        except OSError:
            continue
        if names & {b"codex", b"codex.js"}:
            return True
    return False


# The image ships an empty allowlist. A nonempty one is replaced only when it
# is this script's own policy, and either names the same relay (so only the
# gateway entry is added, which no running member depends on the absence of)
# or names a relay the deployment no longer uses that no sibling Codex member
# is still running against. Anything else -- another server, another shape --
# is never widened or replaced.
document = tomllib.loads(existing)
if document.get("mcp_servers"):
    relay = platform_relay_policy(document)
    if relay is None:
        raise RuntimeError("retained Codex MCP policy is not the platform relay policy")
    if relay != sys.argv[1] and codex_running():
        raise RuntimeError("retained Codex MCP policy belongs to another relay")
fd, temporary = tempfile.mkstemp(prefix=".requirements-", dir=os.path.dirname(POLICY))
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(policy)
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o644)
    os.replace(temporary, POLICY)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
"""
