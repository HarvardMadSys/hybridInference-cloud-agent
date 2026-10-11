"""Package the deployment recipe: everything a host needs, and no source.

An artifact deployment has no checkout. It has images pinned by digest, a
manifest that names them, and this archive — so the archive has to carry the
Compose files, the two deploy scripts, the protocol file and the two helpers
that turn a manifest into a rendered Compose file and an environment fragment.
What it must not carry is source: the Dockerfiles are absent on purpose, because
an image that can be built on the host is an image that can disagree with the
digest the manifest pinned.

The file list is explicit. A directory walk would ship whatever happened to be
there — a developer's `.env`, a stray key, a note with a hostname in it — and
the failure would be a leak nobody reviews. `SECRET_SHAPED` is the second line
of that defence, for the day somebody adds a file to the list without thinking
about it.

Building twice produces the same bytes: fixed timestamps, fixed owners, sorted
names. That matters because the sha256 of this archive goes into a release
manifest, and a hash that changes when nothing changed is a hash nobody can use
to answer "is this the archive that was reviewed".
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import re
import sys
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if __package__:
    from .artifact_compose import recipe_template
else:  # direct execution from the release directory
    from artifact_compose import recipe_template

if TYPE_CHECKING:  # pragma: no cover - annotations only, and they are strings
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Version of the layout below. A deployer refuses a recipe it does not know how
#: to use, which is what makes adding a file a decision rather than a detail.
RECIPE_FORMAT_VERSION = 1

#: Every file the archive carries, and nothing else.
#:
#: No Dockerfiles: images are pulled by digest. No `deploy/local/`: that is the
#: developer stack, and it builds from source by design. No `deploy/preflight/`:
#: it needs httpx and a gateway, and it is a pre-cutover check rather than part
#: of installing a release.
RECIPE_FILES: tuple[str, ...] = (
    "deploy/DEPLOY_PROTOCOL",
    "deploy/watch_peer.py",
    "deploy/control-plane/deploy.sh",
    "deploy/control-plane/docker-compose.yml",
    "deploy/control-plane/docker-compose.compute.yml",
    "deploy/control-plane/compute.example.json",
    "deploy/control-plane/compute-host.env.example",
    "deploy/control-plane/edge.nginx.conf",
    "deploy/control-plane/.env.example",
    "deploy/control-plane/pgadmin-servers.json",
    "deploy/host/deploy.sh",
    "deploy/host/sandbox_host.sh",
    "deploy/host/docker-compose.sandbox.yml",
    "deploy/host/agent-gateway-tunnel.sh",
    "deploy/host/setup_kata_runtime.sh",
    "deploy/host/kata_runtime_epoch.py",
    "deploy/host/.env.example",
    "deploy/reverse-proxy/agents-under-gateway.conf",
    "deploy/release/artifact_compose.py",
    "deploy/release/artifact_env.sh",
    "deploy/release/release_env.py",
    "deploy/release/manifest.schema.json",
    "deploy/release/requirements.txt",
    "deploy/release/README.md",
)

#: Names that never belong in an archive, whatever the list says.
SECRET_SHAPED = re.compile(
    r"(^|/)("
    r"\.env$"  # the file itself
    r"|\.env\.(?!example$|sample$|template$)"  # a variant — but a template is not a secret
    r"|id_[a-z0-9]+$"
    r"|[^/]*\.(pem|key|p12|pfx)$"
    r"|\.netrc$"
    r"|credentials[^/]*$"
    r")",
    re.IGNORECASE,
)


class RecipeError(Exception):
    """Raised when an archive would ship something it must not, or miss something."""


def checked_files(root: Path) -> list[Path]:
    """The listed files, verified to exist and to be safe to ship."""
    missing = [name for name in RECIPE_FILES if not (root / name).is_file()]
    if missing:
        raise RecipeError(
            f"these files are part of the recipe but do not exist: {missing}. A recipe that "
            "silently omits one installs a host that fails later."
        )

    unsafe = [name for name in RECIPE_FILES if SECRET_SHAPED.search(name)]
    if unsafe:
        raise RecipeError(
            f"these entries look like credentials and must not be archived: {unsafe}. "
            "Secrets are supplied by the host's environment, never by a release artifact."
        )
    return [root / name for name in RECIPE_FILES]


def build(root: Path = REPO_ROOT, prefix: str = "recipe") -> bytes:
    """The archive's bytes, deterministic for a given tree."""
    files = checked_files(root)
    buffer = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path in sorted(files):
            info = tarfile.TarInfo(name=f"{prefix}/{path.relative_to(root)}")
            data = path.read_bytes()
            if path.name in {"docker-compose.yml", "docker-compose.sandbox.yml"}:
                stack = "host" if path.name == "docker-compose.sandbox.yml" else "control-plane"
                data = yaml.safe_dump(
                    recipe_template(yaml.safe_load(data), stack), sort_keys=False
                ).encode()
            info.size = len(data)
            # Fixed metadata, so the same tree hashes the same way. Owners are
            # zeroed rather than recorded: the person who built the archive is
            # not part of what a deployment runs.
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if path.suffix == ".sh" or path.name.endswith(".py") else 0o644
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print the sha256 and size a release manifest would carry.",
    )
    arguments = parser.parse_args(argv)

    try:
        payload = build(arguments.root)
    except RecipeError as error:
        print(f"refusing to build a recipe: {error}", file=sys.stderr)
        return 1

    arguments.output.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    if arguments.report:
        print(f"sha256={digest} bytes={len(payload)} files={len(RECIPE_FILES)}")
    else:
        print(f"wrote {arguments.output} ({len(RECIPE_FILES)} files, sha256 {digest[:12]}…)")
    return 0


if __name__ == "__main__":  # pragma: no cover - the CLI is the entry point
    raise SystemExit(main())
