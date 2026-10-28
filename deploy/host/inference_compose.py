"""Remove only the unused inference relay for an explicit local-account host.

Render before Compose interpolates its inputs: inactive profiles still evaluate
required Gateway credentials. The input remains the one reviewed source or
release recipe, and the output stays beside it to preserve all relative paths.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


def local_recipe(document: dict[str, Any]) -> dict[str, Any]:
    """Narrow the existing host recipe without changing any network policy."""
    result = copy.deepcopy(document)
    services = result["services"]
    services.pop("gateway-tunnel")
    for name in ("runner", "sandbox-manager"):
        service = services[name]
        service["environment"]["AGENT_AUTH_MODE"] = "local"
        service["environment"]["AGENT_GATEWAY_URL"] = ""
        dependencies = service.get("depends_on", {})
        dependencies.pop("gateway-tunnel", None)
        if not dependencies:
            service.pop("depends_on", None)
    return result


def main() -> None:
    """Produce a local-only recipe; the caller must select local explicitly."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", required=True, type=Path)
    parser.add_argument("--auth-mode", required=True, choices=("local",))
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.compose.resolve().parent != args.output.resolve().parent:
        parser.error("the local recipe must stay beside its source recipe")
    document = yaml.safe_load(args.compose.read_text())
    with tempfile.NamedTemporaryFile(mode="w", dir=args.output.parent, delete=False) as output:
        output.write(json.dumps(local_recipe(document), indent=2) + "\n")
        temporary = output.name
    os.replace(temporary, args.output)


if __name__ == "__main__":
    main()
