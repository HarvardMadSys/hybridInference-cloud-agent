"""Install Codex's system MCP allowlist inside the retained sandbox.

The host supplies the deployment relay address before starting an agent. The
guest root writes only system policy; member credentials stay in the private
launch environment. No host mount or additional container capability is needed.
"""

from __future__ import annotations

from urllib.parse import urlsplit


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


# Executed as guest root by the trusted host, never through repository code.
# Both directories and an existing policy must be root-owned and unwritable by
# members. Atomic replacement also makes simultaneous member launches safe.
INSTALL_POLICY = r"""
import json
import os
import stat
import sys
import tempfile

for directory in ("/etc", "/etc/codex"):
    info = os.lstat(directory)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError("Codex policy directory is not protected")
target = "/etc/codex/requirements.toml"
info = os.lstat(target)
if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
    raise RuntimeError("Codex system policy is not protected")
policy = "[mcp_servers.session]\nidentity = { url = " + json.dumps(sys.argv[1]) + " }\n"
with open(target, encoding="utf-8") as handle:
    existing = handle.read()
if existing == policy:
    sys.exit(0)
# A nonempty policy must refer to the same deployment. Never silently widen or
# replace another platform policy while a sibling member is using the group.
import tomllib
if tomllib.loads(existing).get("mcp_servers"):
    raise RuntimeError("retained Codex MCP policy belongs to another relay")
fd, temporary = tempfile.mkstemp(prefix=".requirements-", dir="/etc/codex")
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(policy)
        handle.flush()
        os.fsync(handle.fileno())
        os.fchmod(handle.fileno(), 0o644)
    os.replace(temporary, target)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
"""
