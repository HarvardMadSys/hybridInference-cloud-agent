"""Prove the candidate sandbox on the real host before draining live runners.

This process has no enrollment or broker credentials and never polls for work.
Its only sandbox is a temporary, network-isolated guest removed by preflight.
"""

from __future__ import annotations

import os

from cloud_agent_host.sandbox import ContainerBackend, SandboxError, build_backend_from_env
from cloud_agent_host.shared_protocol import (
    SHARED_SANDBOX_HELPER_PINS,
    SHARED_SANDBOX_PROTOCOL,
    SHARED_SANDBOX_RUNTIME_PINS,
)


def check_candidate(backend: ContainerBackend) -> None:
    """Exercise the real kernel/observer and compare the built image's pins."""
    backend.preflight()
    stamp = backend.version_stamp()
    if not stamp.get("sandbox_image_digest"):
        raise SandboxError("candidate image has no immutable digest")
    if stamp.get("shared_sandbox_protocol") != SHARED_SANDBOX_PROTOCOL:
        raise SandboxError("candidate image shared-sandbox protocol does not match the runner")
    for field, expected in (
        ("runtime_versions", SHARED_SANDBOX_RUNTIME_PINS),
        ("helper_versions", SHARED_SANDBOX_HELPER_PINS),
    ):
        actual = stamp.get(field) or {}
        for name, version in expected.items():
            if actual.get(name) != version:
                raise SandboxError(
                    f"candidate {field}.{name}: image={actual.get(name)!r}, runner={version!r}"
                )


def main() -> None:
    """Validate a candidate without any live stack, credentials or user state."""
    backend = build_backend_from_env(
        {
            **os.environ,
            "AGENT_SANDBOX_NETWORK": "none",
            "AGENT_SANDBOX_NAMESPACE": "deploy-candidate",
        }
    )
    if not isinstance(backend, ContainerBackend):
        raise SandboxError("candidate validation requires a container backend")
    check_candidate(backend)
    print("candidate kernel, guest observer and image attestation passed", flush=True)


if __name__ == "__main__":
    main()
