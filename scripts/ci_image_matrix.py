"""Select native image checks without rebuilding unrelated release artifacts.

The complete catalog matches publication. Backend source changes also select the
sandbox: its real-CLI conformance tests execute the control plane and host code,
even though the sandbox Dockerfile does not COPY those Python packages.
"""

from __future__ import annotations

import argparse
import json

ARTIFACTS = [
    {
        "name": "control",
        "arch": "arm64",
        "smoke_args": "--role control",
        "dockerfile": "deploy/control-plane/Dockerfile",
        "runner": '["self-hosted","Linux","ARM64","image-verify-arm64"]',
        "build_args": "",
    },
    {
        # Mount prefixes are compiled into the bundle; check both variants.
        "name": "web-root",
        "arch": "arm64",
        "smoke_args": "--role web --variant root",
        "dockerfile": "deploy/control-plane/Dockerfile.web",
        "runner": '["self-hosted","Linux","ARM64","image-verify-arm64"]',
        "build_args": "NEXT_PUBLIC_BASE_PATH=\nNEXT_PUBLIC_AGENT_API_URL=/api\n",
    },
    {
        "name": "web-agents",
        "arch": "arm64",
        "smoke_args": "--role web --variant agents",
        "dockerfile": "deploy/control-plane/Dockerfile.web",
        "runner": '["self-hosted","Linux","ARM64","image-verify-arm64"]',
        "build_args": "NEXT_PUBLIC_BASE_PATH=/agents\nNEXT_PUBLIC_AGENT_API_URL=/agents/api\n",
    },
    {
        "name": "runner",
        "arch": "amd64",
        "smoke_args": "--role runner",
        "dockerfile": "deploy/host/Dockerfile.agent-runner",
        "runner": '["self-hosted","Linux","X64","image-verify-x64"]',
        "build_args": "",
    },
    {
        "name": "sandbox",
        "arch": "amd64",
        "smoke_args": "--role sandbox",
        "dockerfile": "deploy/sandbox/Dockerfile.agent-sandbox",
        "runner": '["self-hosted","Linux","X64","image-verify-x64"]',
        "build_args": "",
    },
    {
        "name": "tunnel",
        "arch": "amd64",
        "smoke_args": "--role tunnel",
        "dockerfile": "deploy/host/Dockerfile.agent-gateway-tunnel",
        "runner": '["self-hosted","Linux","X64","image-verify-x64"]',
        "build_args": "",
    },
]


def image_matrix(scope: str) -> list[dict[str, str]]:
    """Return the complete catalog unless the classifier proves a narrow scope."""
    selected = {
        "web": {"web-root", "web-agents"},
        "backend": {"control", "runner", "sandbox"},
    }.get(scope)
    return [cell for cell in ARTIFACTS if selected is None or cell["name"] in selected]


def main() -> None:
    """Emit a compact Actions output; an unknown scope conservatively selects all."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("scope")
    args = parser.parse_args()
    print("artifacts=" + json.dumps(image_matrix(args.scope), separators=(",", ":")))


if __name__ == "__main__":
    main()
