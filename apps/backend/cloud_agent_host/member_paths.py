"""Files one member keeps in its private tmpfs for reaching GitHub, and their writer."""

from __future__ import annotations

from typing import TYPE_CHECKING

from cloud_agent_host.chat_sandbox_models import MEMBER_TMPFS_ROOT, MEMBER_UID_BASE
from cloud_agent_shared.errors import SandboxError

if TYPE_CHECKING:
    from cloud_agent_host.chat_sandbox import ChatSandboxManager, ChatSandboxSpec

#: Copies the CA from stdin, then writes the bundle: the image's own roots
#: (whichever system bundle it has) with the relay CA appended.
_WRITE_TRUST = (
    'set -eu; bundle="$1"; ca="$2"; umask 077; '
    'cat > "$ca.tmp.$$"; mv "$ca.tmp.$$" "$ca"; '
    "{ for roots in /etc/ssl/certs/ca-certificates.crt /etc/pki/tls/certs/ca-bundle.crt "
    '/etc/ssl/cert.pem; do if [ -r "$roots" ]; then cat "$roots"; break; fi; done; '
    'cat "$ca"; } > "$bundle.tmp.$$"; mv "$bundle.tmp.$$" "$bundle"'
)


def member_relay_ca_path(slot_id: int) -> str:
    """This deployment's GitHub relay CA, alone (Node adds it to its own roots)."""
    return f"{MEMBER_TMPFS_ROOT}/{slot_id}/github-relay-ca.pem"


def member_ca_bundle_path(slot_id: int) -> str:
    """The sandbox's own trusted roots with the relay CA appended."""
    return f"{MEMBER_TMPFS_ROOT}/{slot_id}/ca-bundle.pem"


def write_member_github_trust(
    manager: ChatSandboxManager,
    spec: ChatSandboxSpec,
    ca_pem: str,
    *,
    slot_id: int,
    user_uid: int,
    user_gid: int,
) -> None:
    """Give one member the GitHub relay CA beside the sandbox's own roots.

    Written as the member's own uid into its private tmpfs, so only its
    processes trust the relay's certificate: the image's trust store and every
    other member are untouched, and other hosts verify exactly as before.
    """
    if not 0 <= slot_id < spec.resident_slot_capacity:
        raise ValueError("resident slot is outside this shared sandbox")
    if user_uid != MEMBER_UID_BASE + slot_id:
        raise ValueError("resident slot does not match its member uid")
    result = manager.exec_capture(
        spec,
        [
            "/bin/sh",
            "-c",
            _WRITE_TRUST,
            "sh",
            member_ca_bundle_path(slot_id),
            member_relay_ca_path(slot_id),
        ],
        workdir="/",
        timeout_s=20,
        input_bytes=ca_pem.encode("ascii"),
        user_uid=user_uid,
        user_gid=user_gid,
    )
    if result.returncode != 0:
        raise SandboxError(
            "member GitHub relay trust could not be written: "
            + result.stderr.decode("utf-8", "replace")[:300]
        )
