"""Embed the reviewed Host build inputs with their control-plane release.

This is a source-root archive for SSH bootstrap, not the artifact deployment
recipe. Only the three backend Python packages and explicitly listed Host build
assets are included. Deployment state, credentials, caches and symlinks never
become an image layer. The builder needs only Python's standard library, so it
also runs in a clean Docker build stage without Git or project dependencies.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import sys
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FORMAT_VERSION = 1
MAX_SOURCE_BYTES = 64 * 1024 * 1024
PACKAGE_ROOTS = (
    "apps/backend/cloud_agent",
    "apps/backend/cloud_agent_host",
    "apps/backend/cloud_agent_shared",
)
HOST_FILES = (
    ".dockerignore",
    "pyproject.toml",
    "uv.lock",
    "README.md",
    "LICENSE",
    "deploy/DEPLOY_PROTOCOL",
    "deploy/host/sandbox_host.sh",
    "deploy/host/setup_kata_runtime.sh",
    "deploy/host/kata_runtime_epoch.py",
    "deploy/host/docker-compose.sandbox.yml",
    "deploy/host/agent-gateway-tunnel.sh",
    "deploy/host/Dockerfile.agent-runner",
    "deploy/host/Dockerfile.agent-gateway-tunnel",
    "deploy/sandbox/Dockerfile.agent-sandbox",
    "deploy/sandbox/build-kilo.sh",
    "deploy/sandbox/kilo-project-config.patch",
    "deploy/sandbox/install-runtimes.sh",
    "deploy/sandbox/guest-observer.h",
    "deploy/sandbox/guest-observer.bpf.c",
    "deploy/sandbox/guest-observer.c",
    "deploy/sandbox/member-window.c",
    "deploy/sandbox/tool-mark.h",
    "deploy/sandbox/tool-mark.c",
    "deploy/sandbox/pi-freeinference",
    "deploy/sandbox/opencode-freeinference",
    "deploy/sandbox/kilo-freeinference",
    "deploy/sandbox/pi-mcp-bridge.ts",
    "deploy/sandbox/http-agent-live-bridge",
    "deploy/sandbox/claude-session-fork",
    "deploy/sandbox/member-process-reaper",
    "deploy/sandbox/codex-requirements.toml",
)
_EXCLUDED_PART = re.compile(
    r"^(?:\.git|\.compute-secrets|\.env(?:\..*)?|__pycache__|"
    r"\.venv|venv|node_modules|\.pytest_cache|\.ruff_cache|\.mypy_cache|"
    r"id_[a-z0-9]+|credentials(?:\..*)?|api_key(?:\..*)?)$",
    re.IGNORECASE,
)
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".crt"}


class HostBundleError(Exception):
    """The available source cannot be published as a reviewed Host bundle."""


def _excluded(name: str) -> bool:
    return bool(_EXCLUDED_PART.fullmatch(name)) or Path(name).suffix.lower() in _SECRET_SUFFIXES


def _checked_file(root: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise HostBundleError("Host bundle contains an unsafe source path")
    if any(_excluded(part) for part in relative.parts):
        raise HostBundleError("Host bundle contains a credential or deployment-state path")
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise HostBundleError(f"Host bundle source must not be a symlink: {name}")
    if not path.is_file():
        raise HostBundleError(f"Host bundle source is missing or not a regular file: {name}")
    return path


def source_files(root: Path) -> list[Path]:
    """Select only build assets and Python source within the approved packages."""
    names = set(HOST_FILES)
    for package in PACKAGE_ROOTS:
        _checked_file(root, f"{package}/__init__.py")
        for directory, children, files in os.walk(root / package, followlinks=False):
            children[:] = sorted(child for child in children if not _excluded(child))
            for child in children:
                if (Path(directory) / child).is_symlink():
                    raise HostBundleError(
                        "Host bundle packages must not contain symlink directories"
                    )
            for name in files:
                if name.endswith(".py") and not _excluded(name):
                    names.add((Path(directory) / name).relative_to(root).as_posix())
    return [_checked_file(root, name) for name in sorted(names)]


def build(root: Path = REPO_ROOT) -> bytes:
    """Produce deterministic gzip/tar bytes with no containing directory."""
    output = io.BytesIO()
    total = 0
    with (
        gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed,
        tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive,
    ):
        for path in source_files(root):
            data = path.read_bytes()
            total += len(data)
            if total > MAX_SOURCE_BYTES:
                raise HostBundleError("Host bundle source exceeds the size limit")
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size = len(data)
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mode = 0o755 if path.suffix == ".sh" or data.startswith(b"#!") else 0o644
            archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def write_release(
    root: Path, output: Path, source_commit: str, *, allow_unknown: bool = False
) -> bool:
    """Publish the archive and its exact source revision, or an unavailable manual build."""
    if allow_unknown and source_commit == "unknown":
        output.mkdir(parents=True, exist_ok=True)
        for name in ("source.tar.gz", "release.json"):
            (output / name).unlink(missing_ok=True)
        return False
    if not re.fullmatch(r"[a-f0-9]{40}", source_commit):
        raise HostBundleError("Host bundle release requires an exact 40-character source commit")
    bundle = build(root)
    manifest = {
        "format_version": FORMAT_VERSION,
        "source_commit": source_commit,
        "source_sha256": hashlib.sha256(bundle).hexdigest(),
        "bundle": "source.tar.gz",
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "source.tar.gz").write_bytes(bundle)
    (output / "release.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--allow-unknown", action="store_true")
    args = parser.parse_args(argv)
    try:
        available = write_release(
            args.root, args.output, args.source_commit, allow_unknown=args.allow_unknown
        )
    except (HostBundleError, OSError) as error:
        print(f"refusing to publish Host bundle: {error}", file=sys.stderr)
        return 1
    print(
        "embedded reviewed Host release"
        if available
        else "Host release unavailable: unknown commit"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
