"""Verified releases on this machine: import with a pinned digest, check again before use.

A release enters the lock only through ``import_release``: the operator (or a
downstream distribution's reviewed lock file named in the local policy) pins
the SHA-256 of the manifest's exact bytes; the manifest must pass the
published schema, and the recipe archive must match the digest the manifest
names. The control plane can only choose among releases already in the lock;
it can never point the agent at a URL. Before a phase runs, both digests are
checked again, so a file changed on disk after import is refused.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tarfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from deploy.agent.store import AgentStore, StoreError, write_json, write_private

if TYPE_CHECKING:
    from deploy.agent.system import CommandResult, CommandRunner

#: Which manifest artifacts each machine role runs.
ROLE_ARTIFACTS = {"control-plane": ("control", "web"), "host": ("runner", "sandbox", "tunnel")}
#: The recipe directory each role deploys from, and its phases in order.
ROLE_DIRECTORY = {"control-plane": "control-plane", "host": "host"}


class ReleaseError(RuntimeError):
    """A release cannot be imported or used; the message names no secret."""


def sha256_file(path: Path) -> str:
    """The SHA-256 of a file's bytes."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _pinned(store: AgentStore, manifest_sha256: str, pin: str | None) -> str:
    """Where the pin came from, or refuse: an operator's value or the policy's lock file."""
    if pin:
        if pin != manifest_sha256:
            raise ReleaseError("the manifest's SHA-256 does not match the pinned value")
        return "operator"
    lock_path = store.policy().get("release_lock_file")
    if lock_path:
        try:
            lock = json.loads(Path(lock_path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ReleaseError("the policy's release lock file cannot be read") from exc
        pinned = lock.get("manifests") if isinstance(lock, dict) else None
        if isinstance(pinned, list) and manifest_sha256 in pinned:
            return "lock-file"
    raise ReleaseError("pin the manifest's SHA-256, or list it in the policy's release lock file")


def import_release(
    store: AgentStore, manifest_path: Path, recipe_path: Path, pin: str | None = None
) -> dict[str, Any]:
    """Verify and copy a release into this machine's lock; returns its lock entry."""
    manifest_bytes = manifest_path.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
    source = _pinned(store, manifest_sha256, pin)
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise ReleaseError("the manifest is not JSON") from exc
    try:
        from deploy.release.release_env import ReleaseEnvError, validate_manifest
    except ImportError as exc:  # pragma: no cover - the recipe ships it
        raise ReleaseError("deploy/release/release_env.py is missing from this recipe") from exc
    try:
        validate_manifest(manifest)
    except ReleaseEnvError as exc:
        raise ReleaseError(f"the manifest does not match the published schema: {exc}") from exc
    recipe_sha256 = sha256_file(recipe_path)
    if recipe_sha256 != manifest["recipe"]["sha256"]:
        raise ReleaseError("the recipe archive does not match the digest the manifest names")
    target = store.release_dir(manifest_sha256)
    staging = target.with_name(target.name + ".staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    write_private(staging / "release.json", manifest_bytes, 0o644)
    shutil.copyfile(recipe_path, staging / "recipe.tar.gz")
    with tarfile.open(staging / "recipe.tar.gz") as archive:
        archive.extractall(staging / "extracted", filter="data")
    shutil.rmtree(target, ignore_errors=True)
    staging.rename(target)
    entry = {
        "source_commit": manifest["source_commit"],
        "database_revision": manifest.get("database_revision"),
        "deploy_protocol": manifest.get("deploy_protocol"),
        "recipe_sha256": recipe_sha256,
        "verified_upgrade_from": manifest.get("compatibility", {}).get("verified_upgrade_from", []),
        "verified_rollback_to": manifest.get("compatibility", {}).get("verified_rollback_to", []),
        "pinned_by": source,
        "imported_at": time.time(),
    }
    lock = store.release_lock()
    lock[manifest_sha256] = entry
    store.save_release_lock(lock)
    return {"manifest_sha256": manifest_sha256, **entry}


def verified_release(store: AgentStore, manifest_sha256: str) -> tuple[Path, dict[str, Any]]:
    """The extracted recipe of a locked release, after checking both digests again."""
    entry = store.release_lock().get(manifest_sha256)
    if entry is None:
        raise ReleaseError("this release is not in this machine's verified lock")
    directory = store.release_dir(manifest_sha256)
    try:
        if sha256_file(directory / "release.json") != manifest_sha256:
            raise ReleaseError("the stored manifest changed after it was imported")
        if sha256_file(directory / "recipe.tar.gz") != entry["recipe_sha256"]:
            raise ReleaseError("the stored recipe changed after it was imported")
    except FileNotFoundError as exc:
        raise ReleaseError("the stored release is incomplete; import it again") from exc
    manifest = json.loads((directory / "release.json").read_text())
    return directory, manifest


def deployed_commit(store: AgentStore) -> str:
    """What the deployment says it runs (``.deployed-sha``), empty when unknown."""
    try:
        return (store.deploy_root / ".deployed-sha").read_text().strip()
    except FileNotFoundError:
        return ""


def run_phase(
    store: AgentStore,
    runner: CommandRunner,
    *,
    role: str,
    manifest_sha256: str,
    phase: str,
    draining_host: str | None = None,
) -> tuple[CommandResult, dict[str, Any]]:
    """Run the release recipe's own ``deploy.sh --phase`` for this machine."""
    directory, manifest = verified_release(store, manifest_sha256)
    script = directory / "extracted" / "recipe" / "deploy" / ROLE_DIRECTORY[role] / "deploy.sh"
    if not script.is_file():
        raise ReleaseError("the recipe has no deploy script for this machine's role")
    policy = store.policy()
    env = {
        "AGENT_DEPLOY_MODE": "artifacts",
        "AGENT_RELEASE_MANIFEST": str(directory / "release.json"),
        "AGENT_DEPLOY_PYTHON": str(policy.get("python") or "python3"),
        "DEPLOY_ROOT": str(store.deploy_root),
    }
    if draining_host:
        env["AGENT_DEPLOY_DRAINING_HOST"] = draining_host
    timeout = float(policy.get("phase_timeout_s") or 3600)
    result = runner.run(
        ["bash", str(script), "--phase", phase], cwd=store.deploy_root, env=env, timeout=timeout
    )
    return result, manifest


def images_present(
    runner: CommandRunner, store: AgentStore, manifest: dict[str, Any], role: str
) -> list[str]:
    """Images of this role's artifacts that are not present locally (empty: all are)."""
    docker = str((store.policy().get("compose") or {}).get("docker") or "docker")
    missing = []
    for artifact in manifest.get("artifacts", []):
        if artifact.get("role") not in ROLE_ARTIFACTS[role]:
            continue
        result = runner.run([docker, "image", "inspect", artifact["image"]], cwd=store.deploy_root)
        if not result.ok:
            missing.append(artifact["image"])
    return missing


def _revision_number(revision: Any) -> int | None:
    head = str(revision or "").split("_", 1)[0]
    return int(head) if head.isdigit() else None


def rollback_allowed(store: AgentStore, target_sha256: str) -> str | None:
    """Why moving to ``target_sha256`` is refused as an unverified downgrade, or ``None``.

    A release whose database revision is older than the running release's
    would run against newer migrations; the backup and recovery runbook says
    a previous release is not automatically compatible with them. Such a move
    is allowed only when the running release lists the target commit in
    ``verified_rollback_to`` or the target lists the running commit in
    ``verified_upgrade_from``. An empty list means the path was never verified.
    """
    lock = store.release_lock()
    target = lock.get(target_sha256)
    if target is None:
        return "the release is not in this machine's verified lock"
    running = deployed_commit(store)
    if not running or running == target["source_commit"]:
        return None
    current = next((e for e in lock.values() if e["source_commit"] == running), None)
    if current is None:
        return None
    now, then = (
        _revision_number(current.get("database_revision")),
        _revision_number(target.get("database_revision")),
    )
    if now is None or then is None or then >= now:
        return None
    if running in target.get("verified_upgrade_from", []) or target["source_commit"] in current.get(
        "verified_rollback_to", []
    ):
        return None
    return (
        "this release has an older database revision than the running one, and moving back "
        "to it was never verified"
    )


def forget_release(store: AgentStore, manifest_sha256: str) -> None:
    """Remove a release from the lock and the disk (local command only)."""
    lock = store.release_lock()
    if manifest_sha256 not in lock:
        raise StoreError("this release is not in the lock")
    del lock[manifest_sha256]
    store.save_release_lock(lock)
    shutil.rmtree(store.release_dir(manifest_sha256), ignore_errors=True)
    write_json(store.root / "releases" / "forgotten.json", {"last": manifest_sha256}, 0o644)


__all__ = [
    "ReleaseError",
    "deployed_commit",
    "forget_release",
    "images_present",
    "import_release",
    "rollback_allowed",
    "run_phase",
    "sha256_file",
    "verified_release",
]
