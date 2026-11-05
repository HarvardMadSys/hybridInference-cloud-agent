"""Every process in a sandbox trusts the GitHub relay's certificate, credential or not.

With the relay configured, a group bridge resolves ``github.com`` and
``api.github.com`` to the relay for *every* process on it: a member with a
placeholder, a member of a temporary workspace (which has none), an
environment's setup, services or verification script, an action, a terminal.
The relay serves all of them; a request without its placeholder goes to GitHub
as nobody, exactly as it would have gone directly. So trusting the relay's
certificate cannot depend on holding a GitHub credential: a setup script's
``git clone`` of a public repository would otherwise fail its TLS check.

The trust is the sandbox's, not a member's. A root exec writes the CA and a
bundle of the image's own roots with the CA appended under :data:`TRUST_DIR`,
root-owned and ``0444``, before the first process starts; no member, script
or terminal can change it. The CA is name-constrained to the relay's two names
(``deploy/control-plane/github-relay-certs.sh``), so trusting it vouches for
nothing else. The image's own trust store is left as it is.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from cloud_agent_host.github_relay_env import relay_ca_pem
from cloud_agent_host.install_locks import install_lock
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

logger = logging.getLogger(__name__)

TRUST_DIR = "/etc/freeinference"
#: This deployment's GitHub relay CA, alone (Node adds it to its own roots).
RELAY_CA_PATH = f"{TRUST_DIR}/github-relay-ca.pem"
#: The image's own roots with the relay CA appended.
CA_BUNDLE_PATH = f"{TRUST_DIR}/ca-bundle.pem"

#: Copies the CA from stdin, then writes the bundle: the image's own roots
#: (whichever system bundle it has), a newline in case they do not end in one,
#: and the relay CA. Each file is complete before it is renamed into place.
_INSTALL = (
    'set -eu; dir="$1"; ca="$2"; bundle="$3"; umask 022; mkdir -p "$dir"; '
    'cat > "$ca.tmp.$$"; '
    "{ for roots in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt "
    '/etc/ssl/cert.pem; do if [ -r "$roots" ]; then cat "$roots"; printf "\\n"; break; fi; '
    'done; cat "$ca.tmp.$$"; } > "$bundle.tmp.$$"; '
    'chmod 0444 "$ca.tmp.$$" "$bundle.tmp.$$"; '
    'mv -f "$ca.tmp.$$" "$ca"; mv -f "$bundle.tmp.$$" "$bundle"'
)


def trust_key(ca_pem: str) -> str:
    """What a container is remembered to hold: the bundle, for exactly this CA.

    A rotated CA has another digest, so a container that holds the old one is
    written again rather than taken as current.
    """
    return f"{CA_BUNDLE_PATH}@{hashlib.sha256(ca_pem.encode('ascii')).hexdigest()}"


def trust_env() -> dict[str, str]:
    """The variables that point a process's TLS clients at the bundle.

    Go (``gh``) and OpenSSL read ``SSL_CERT_FILE``; git's libcurl and the curl
    CLI are told explicitly, as are Python requests and Node. A process's own
    value for any of them wins: these are defaults, not policy.
    """
    return {
        "SSL_CERT_FILE": CA_BUNDLE_PATH,
        "GIT_SSL_CAINFO": CA_BUNDLE_PATH,
        "CURL_CA_BUNDLE": CA_BUNDLE_PATH,
        "REQUESTS_CA_BUNDLE": CA_BUNDLE_PATH,
        "NODE_EXTRA_CA_CERTS": RELAY_CA_PATH,
    }


def install_github_relay_trust(
    manager: ChatSandboxManager, spec: ChatSandboxSpec, ca_pem: str
) -> None:
    """Write the relay CA and the bundle into the running container, as root."""
    result = manager.exec_capture(
        spec,
        ["/bin/sh", "-c", _INSTALL, "sh", TRUST_DIR, RELAY_CA_PATH, CA_BUNDLE_PATH],
        workdir="/",
        timeout_s=20,
        input_bytes=ca_pem.encode("ascii"),
        user_uid=0,
        user_gid=0,
    )
    if result.returncode != 0:
        raise SandboxError(
            "the GitHub relay's trust could not be installed: "
            + result.stderr.decode("utf-8", "replace")[:300]
        )


def ensure_github_relay_trust(manager: ChatSandboxManager, spec: ChatSandboxSpec) -> dict[str, str]:
    """Have the container trust the GitHub relay, and say how a process uses it.

    Written once per container and CA, then remembered like a root helper
    (``ProvenContainers.note_helper``). Without a relay configured there is
    nothing to trust and nothing changes. A failed install leaves the process
    on the image's own roots, as without the relay, rather than refusing to
    start it, and the next process tries again.
    """
    ca_pem = relay_ca_pem()
    if ca_pem is None:
        return {}
    key = trust_key(ca_pem)
    proven = manager._proven.get(spec)
    if manager._proven.holds_helper(proven, key):
        return trust_env()
    with install_lock(spec.container_name):
        if not manager._proven.holds_helper(proven, key):
            try:
                install_github_relay_trust(manager, spec, ca_pem)
            except SandboxError as exc:
                logger.warning(
                    "github_relay_trust_install_failed",
                    extra={"sandbox_id": spec.sandbox_id, "detail": str(exc)[:300]},
                )
                return {}
            manager._proven.note_helper(proven, key)
    return trust_env()
