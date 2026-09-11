"""Turn a source Compose file into the one a release deploys.

The release recipe must not contain `build:`, and the reason is not tidiness:
Compose interpolates the whole file before it decides anything else. The console
service's `build.args` carry a required value —
`NEXT_PUBLIC_AGENT_API_URL: ${AGENT_WEB_API_URL:?...}` — so a deployment that has
the image and no build arguments fails to *parse*. `up --no-build` does not skip
interpolation, and the deploy path is not one command anyway: migrations and
readiness run through `compose run --no-deps`.

So one file is maintained and two are produced. This generator sets each
service's image from the release manifest and deletes its `build`, and it refuses
to write a result in which either half of that is untrue. `test_artifact_compose.py`
compares the output against the source to show that nothing else moved: the same
services, the same ports, mounts, networks and dependencies.

Comments do not survive the round trip through YAML, which is why the generated
file is written next to the recipe's own README rather than published as a
document — the annotated source stays the place to read about a service.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:  # pragma: no cover - annotations only, and they are strings
    from collections.abc import Mapping, Sequence

#: Which release role supplies each service, per stack.
#:
#: One image answers several services on purpose — the runner image runs the
#: broker and the egress config renderer, the tunnel image runs all three
#: tunnels — and the manifest carries one entry per role, so this table is what
#: keeps "six artifacts" from becoming "six images and eleven questions".
STACKS: dict[str, dict[str, tuple[str, ...]]] = {
    "control-plane": {
        "control": ("migrate", "control-plane", "mcp-relay"),
        "web": ("web",),
    },
    "host": {
        "runner": ("runner", "workspace-broker", "agent-egress-proxy-config"),
        "tunnel": ("gateway-tunnel", "mcp-tunnel", "control-plane-tunnel"),
        "egress": ("agent-egress-proxy",),
    },
}

#: The web variant each stack's web service is built for. The control plane
#: stack serves one mount shape; which one is a deployment's decision, and it
#: has to match the image the manifest names.
WEB_VARIANTS = ("root", "agents")

_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")


class ComposeError(Exception):
    """Raised when a stack cannot be rendered from this manifest."""


def image_placeholder(role: str) -> str:
    """The only unbound image reference permitted in a packaged recipe."""
    return f"${{AGENT_{role.upper()}_IMAGE:?release manifest must supply {role}}}"


def recipe_template(document: Mapping[str, Any], stack: str) -> dict[str, Any]:
    """Remove builds before archiving; bind the release's digests when deploying."""
    return render(
        document,
        stack,
        {role: image_placeholder(role) for role in STACKS[stack]},
        "root" if stack == "control-plane" else None,
    )


def images_from_manifest(manifest: Mapping[str, Any], variant: str | None = None) -> dict[str, str]:
    """The digest reference for each role this manifest carries.

    `variant` selects the web image. A deployment serves one mount shape, and
    the two are different images — `/agents` and `/` are not interchangeable at
    runtime, which is the whole reason the manifest names both.
    """
    if variant is not None and variant not in WEB_VARIANTS:
        raise ComposeError(f"unknown web variant {variant!r}; expected one of {WEB_VARIANTS}")

    images: dict[str, str] = {}
    for artifact in manifest.get("artifacts") or ():
        role = artifact.get("role")
        if role == "web" and artifact.get("variant") != variant:
            # A deployment serves one mount shape; the other image is for a
            # different deployment of the same release.
            continue
        reference = artifact.get("image", "")
        if not _IMAGE_RE.fullmatch(reference):
            raise ComposeError(
                f"artifact {role!r} is {reference!r}: a release deploys digests, and a "
                "recipe generated from a tag would deploy whatever that tag points at"
            )
        if role in images:
            raise ComposeError(f"artifact {role!r} appears twice in the manifest")
        images[role] = reference

    # Per-stack completeness is checked in `render`, where the stack is known:
    # a host deployment needs no web image, and a control-plane deployment that
    # has no web image is the case that must fail.
    return images


def render(
    document: Mapping[str, Any], stack: str, images: Mapping[str, str], variant: str | None
) -> dict[str, Any]:
    """Set the images, drop the builds, and refuse anything else that moved."""
    if stack not in STACKS:
        raise ComposeError(f"unknown stack {stack!r}; this generator knows {sorted(STACKS)}")

    services = document.get("services")
    if not isinstance(services, dict) or not services:
        raise ComposeError("the source Compose file declares no services")

    rendered = yaml.safe_load(yaml.safe_dump(document, sort_keys=False))
    for role, names in STACKS[stack].items():
        if role == "web" and variant is None:
            raise ComposeError("the control-plane stack needs --variant: root or agents")
        if role not in images:
            raise ComposeError(
                f"the manifest carries no {role!r} artifact, and this stack runs "
                f"{list(names)} from it"
            )
        for name in names:
            service = services.get(name)
            if service is None:
                raise ComposeError(
                    f"service {name!r} is mapped to role {role!r} but the Compose file has "
                    "no such service; the mapping and the stack have drifted apart"
                )
            if "build" not in service and service.get("image") != image_placeholder(role):
                raise ComposeError(
                    f"service {name!r} has neither a source build nor the recipe's {role!r} "
                    "image placeholder; the mapping and the stack have drifted apart"
                )
            rendered["services"][name]["image"] = images[role]
            rendered["services"][name].pop("build", None)

    leftovers = sorted(
        name
        for name, service in rendered["services"].items()
        if isinstance(service, dict) and "build" in service
    )
    if leftovers:
        raise ComposeError(
            f"these services still carry `build`, so the release recipe would depend on "
            f"source: {leftovers}. Add them to STACKS under the role that supplies them."
        )
    return rendered


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--compose", type=Path, required=True)
    parser.add_argument("--stack", required=True, choices=sorted(STACKS))
    parser.add_argument("--variant", choices=WEB_VARIANTS, default=None)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args(argv)

    try:
        manifest = json.loads(arguments.manifest.read_text())
        source = yaml.safe_load(arguments.compose.read_text())
        images = images_from_manifest(manifest, arguments.variant)
        rendered = render(source, arguments.stack, images, arguments.variant)
    except (ComposeError, yaml.YAMLError, json.JSONDecodeError) as error:
        print(f"refusing to write a release Compose file: {error}", file=sys.stderr)
        return 1

    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(yaml.safe_dump(rendered, sort_keys=False))
    print(
        f"wrote {arguments.output} for {arguments.stack}: "
        f"{len(rendered['services'])} services, no build left"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is the entry point
    raise SystemExit(main())
