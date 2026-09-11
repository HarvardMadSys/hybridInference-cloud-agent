"""The values an artifact deployment takes from its release, and nothing else.

A deployment in artifact mode has no source checkout, so it cannot derive any of
this: which image each role runs, which commit those images contain, and which
mount shape the console was built for. All of it comes from the release manifest
— and that is a security property, not a convenience. A host `.env` that could
name a different image, or a different SHA, would let the machine that is *being
deployed* choose what runs on it, and the exact-SHA readiness fence would then
be comparing a value the deployment set against a value the deployment set.

So this module emits a small environment file for the deploy scripts to source
*after* their own `.env`, and it refuses when the ambient environment already
disagrees: an operator who set `AGENT_CONTROL_IMAGE` by hand meant something by
it, and silently overriding them is how a deployment ends up somewhere nobody
intended.

Values are validated before they are written, because these are shell variables
and the manifest is a document from somewhere else. Nothing here may contain a
character the shell would treat as syntax.

The document is checked against the published schema first, and the values a
host has already configured are passed in explicitly — from the same dotenv
files the deploy script loads — because `os.environ` never sees a shell
variable, and a conflict that cannot be seen cannot be refused.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - annotations only, and they are strings
    from collections.abc import Mapping, Sequence

#: The published contract this module checks a manifest against before mapping it.
SCHEMA_PATH = Path(__file__).resolve().parent / "manifest.schema.json"

_IMAGE_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")

#: The mount shape each variant is built for. A variant and a base path are one
#: decision: `agents` with an empty base path is a deployment whose health check
#: probes `/` while its image serves `/agents`, and both halves look valid on
#: their own. The manifest schema states this pairing too; it is repeated here
#: because this is the code that would act on it.
WEB_BASE_PATHS = {"root": "", "agents": "/agents"}
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

#: Which manifest roles become which variable, per stack. The names match the
#: ones the Compose files already interpolate, so a generated release Compose
#: file picks them up without a second translation.
STACK_ROLES: dict[str, dict[str, str]] = {
    "control-plane": {
        "control": "AGENT_CONTROL_IMAGE",
        "web": "AGENT_WEB_IMAGE",
    },
    "host": {
        "runner": "AGENT_RUNNER_IMAGE",
        "tunnel": "AGENT_TUNNEL_IMAGE",
        "egress": "AGENT_EGRESS_IMAGE",
        "sandbox": "AGENT_SANDBOX_IMAGE",
    },
}


class ReleaseEnvError(Exception):
    """Raised when a manifest cannot configure a deployment."""


def _checked(name: str, value: str, pattern: re.Pattern[str], what: str) -> str:
    if not pattern.fullmatch(value or ""):
        raise ReleaseEnvError(f"{name} is {value!r}, which is not {what}")
    return value


def parsed_ambient(pairs: Sequence[str]) -> dict[str, str]:
    """The **resolved** environment, as `docker compose config --environment` prints it.

    Not a dotenv file, and deliberately so. Compose resolves interpolation by its
    own rules — an exported variable beats a `.env` entry, `$VAR` and `${VAR}`
    both expand, and a value may be built from another one in the same file.
    Reading `.env` here would be a second implementation of those rules, and the
    first thing it got wrong was the precedence: this module let the file win,
    Compose lets the environment win, so a host that named another image passed
    the check and then deployed it.

    Values are already resolved; quotes in the output belong to the value.
    Removing them would accept an explicitly configured, invalid image or SHA.
    """
    values: dict[str, str] = {}
    for pair in pairs:
        key, separator, value = pair.partition("=")
        if not separator or not key:
            raise ReleaseEnvError(f"--ambient expects KEY=VALUE, got {pair!r}")
        values[key] = value
    return values


def validate_manifest(manifest: Mapping[str, Any], schema_path: Path | None = None) -> None:
    """Refuse a document that is not a release manifest at all.

    The mapping below needs four fields; the document has ten, and the ones it
    does not read — the protocol, the recipe, the runtime catalog — are what a
    deployment is entitled to assume. Treating a partial document as a manifest
    is how a deployment ends up running images from a release that was never
    published as one.

    `jsonschema` is a development dependency, so a deploy host needs it named in
    the recipe's requirements; an environment that cannot import it refuses
    rather than skipping the check.
    """
    path = schema_path or SCHEMA_PATH
    try:
        import jsonschema
    except ImportError as error:  # pragma: no cover - the deploy host installs it
        raise ReleaseEnvError(
            f"jsonschema is not installed, so this manifest cannot be checked against "
            f"{path.name}. Install the deployment requirements before deploying; an "
            "unchecked manifest is not a release."
        ) from error

    schema = json.loads(path.read_text())
    try:
        jsonschema.validate(instance=dict(manifest), schema=schema)
    except jsonschema.ValidationError as error:
        where = "/".join(str(part) for part in error.absolute_path) or "<document>"
        raise ReleaseEnvError(
            f"this is not a release manifest: {path.name} rejects it at {where}: {error.message}"
        ) from error


def environment_for(
    manifest: Mapping[str, Any],
    stack: str,
    variant: str | None = None,
    ambient: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The variables this stack deploys with, checked against the ambient one."""
    if stack not in STACK_ROLES:
        raise ReleaseEnvError(f"unknown stack {stack!r}; expected one of {sorted(STACK_ROLES)}")

    commit = _checked(
        "source_commit", str(manifest.get("source_commit", "")), _COMMIT_RE, "a full commit"
    )

    by_role: dict[str, dict[str, Any]] = {}
    for artifact in manifest.get("artifacts") or ():
        role = artifact.get("role")
        if role == "web" and artifact.get("variant") != variant:
            continue
        if role in by_role:
            raise ReleaseEnvError(f"the manifest carries two {role!r} artifacts")
        by_role[role] = artifact

    environment: dict[str, str] = {"AGENT_DEPLOYMENT_SHA": commit}
    for role, variable in STACK_ROLES[stack].items():
        artifact = by_role.get(role)
        if artifact is None:
            wanted = f" for variant {variant!r}" if role == "web" else ""
            raise ReleaseEnvError(
                f"the manifest carries no {role!r} artifact{wanted}, and this stack runs it"
            )
        environment[variable] = _checked(
            variable, str(artifact.get("image", "")), _IMAGE_RE, "an image digest reference"
        )

    if stack == "control-plane":
        if variant is None:
            raise ReleaseEnvError("the control-plane stack needs --variant: root or agents")
        base_path = by_role["web"].get("base_path")
        expected = WEB_BASE_PATHS[variant]
        if base_path != expected:
            raise ReleaseEnvError(
                f"the manifest's web artifact is variant {variant!r} with base path "
                f"{base_path!r}, and {variant!r} is built for {expected!r}. The two are one "
                "decision: a mismatch probes a mount point the image does not serve."
            )
        environment["AGENT_WEB_VARIANT"] = variant
        # Quoted so an empty base path survives `source` as an empty string
        # rather than disappearing — the health check probes `/${WEB_BASE_PATH}`.
        environment["AGENT_WEB_BASE_PATH"] = f'"{base_path}"'

    for key, value in environment.items():
        found = (ambient or {}).get(key)
        if found is not None and found != value.strip('"'):
            raise ReleaseEnvError(
                f"{key} is already set to {found!r} in this environment, and this release "
                f"deploys {value!r}. A host does not get to choose what runs on it: remove the "
                "variable, or deploy the release that names it."
            )
    return environment


def render(environment: Mapping[str, str]) -> str:
    """A shell fragment, validated above, safe to `source`.

    `export`, not a bare assignment: the readiness probe, `deploy.sh`'s own
    `from_env` lookups and Compose itself all read these from the environment of
    a child process, and a shell variable that was never exported is invisible
    to every one of them. Sourcing a fragment of plain assignments produced a
    deployment whose SHA was set for the script and unset for the stack.
    """
    return "".join(f"export {key}={value}\n" for key, value in sorted(environment.items()))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--stack", required=True, choices=sorted(STACK_ROLES))
    parser.add_argument("--variant", choices=("root", "agents"), default=None)
    parser.add_argument(
        "--ambient",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="One line of `docker compose config --environment` output. The deploy scripts "
        "pass what Compose resolved, so the check and the deployment read the same values "
        "under the same rules. Repeatable.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the fragment here. Omit to print it, which is what a person debugging a "
        "manifest wants and what a deploy script must not do (its stdout is a log).",
    )
    arguments = parser.parse_args(argv)

    try:
        manifest = json.loads(arguments.manifest.read_text())
        validate_manifest(manifest)
        # `os.environ` is a fallback, not an authority: what Compose resolved is
        # what the deployment will use, and where the two disagree the resolved
        # value is the one to compare against.
        ambient = {**os.environ, **parsed_ambient(arguments.ambient)}
        environment = environment_for(manifest, arguments.stack, arguments.variant, ambient=ambient)
    except (ReleaseEnvError, json.JSONDecodeError) as error:
        print(f"refusing to configure this deployment: {error}", file=sys.stderr)
        return 1

    fragment = render(environment)
    if arguments.output:
        arguments.output.write_text(fragment)
        print(f"wrote {arguments.output}: {len(environment)} values from the release manifest")
    else:
        sys.stdout.write(fragment)
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is the entry point
    raise SystemExit(main())
