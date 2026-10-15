"""Owner environment configuration, isolated tests and workspace operations."""

from __future__ import annotations

from typing import Any, Literal

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cloud_agent.api._owner_auth import require_agent_owner
from cloud_agent.api._workspace_access import fence_workspace_start
from cloud_agent.api.deps import (
    get_agent_app_credentials,
    get_gateway_client,
    get_storage,
    require_agent_access,
    require_store,
)
from cloud_agent.api.worker import verify_claiming_host
from cloud_agent.entitlement import RepoNotAllowed, require_entitled_repo
from cloud_agent.environments import (
    EnvironmentBusy,
    EnvironmentConflict,
    EnvironmentEncryptionError,
    EnvironmentNotFound,
)
from cloud_agent.sandbox_manager_client import SandboxManagerError
from cloud_agent.schemas import REPO_PATTERN
from cloud_agent_shared.environment import (
    ENVIRONMENT_CAPABILITY,
    MAX_IMPORT_BYTES,
    MIN_SECRET_CHARS,
    EnvironmentInputError,
    EnvironmentScripts,
    VariableEdit,
    environment_payload,
    parse_env,
)
from cloud_agent_shared.environment_snapshots import ENVIRONMENT_SNAPSHOT_CAPABILITY


class CredentialSafeRoute(APIRoute):
    """Keep submitted secrets out of validation errors and generic failures."""

    def get_route_handler(self) -> Any:
        """Wrap environment inputs in safe, non-cacheable error responses."""
        original = super().get_route_handler()

        async def safe(request: Request) -> Response:
            try:
                response = await original(request)
            except RequestValidationError as exc:
                errors = []
                for error in exc.errors():
                    message = error["msg"]
                    cause = error.get("ctx", {}).get("error")
                    if isinstance(cause, EnvironmentInputError):
                        message = str(cause)
                    elif error["type"] in {"value_error", "assertion_error"}:
                        message = "Invalid field value"
                    errors.append({"loc": error["loc"], "type": error["type"], "msg": message})
                detail: Any = errors
                if any(
                    "execution_profile is server-owned" in e.get("msg", "") for e in exc.errors()
                ):
                    detail = "execution_profile is server-owned"
                response = JSONResponse(status_code=422, content={"detail": detail})
            except HTTPException as exc:
                response = JSONResponse(
                    status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
                )
            except EnvironmentNotFound:
                response = JSONResponse(
                    status_code=404, content={"detail": "Environment or run not found."}
                )
            except SandboxManagerError as exc:
                response = JSONResponse(
                    status_code=exc.status_code, content={"detail": exc.message}
                )
            except asyncpg.UniqueViolationError:
                response = JSONResponse(
                    status_code=409,
                    content={"detail": "Configuration conflicts with an existing object."},
                )
            except (EnvironmentConflict, EnvironmentBusy) as exc:
                response = JSONResponse(
                    status_code=409,
                    content={
                        "detail": str(exc),
                        **(
                            {"current_revision": exc.current_revision}
                            if isinstance(exc, EnvironmentConflict)
                            and exc.current_revision is not None
                            else {}
                        ),
                    },
                )
            except EnvironmentEncryptionError:
                response = JSONResponse(
                    status_code=503,
                    content={
                        "detail": "Environment encryption is unavailable. Contact the deployment administrator."
                    },
                )
            except ValidationError:
                response = JSONResponse(
                    status_code=422, content={"detail": "Invalid environment variable or script"}
                )
            except EnvironmentInputError as exc:
                response = JSONResponse(status_code=422, content={"detail": str(exc)})
            response.headers["Cache-Control"] = "no-store"
            return response

        return safe


router = APIRouter(prefix="/v1/agent", tags=["environments"], route_class=CredentialSafeRoute)


class RevisionInput(BaseModel):
    """The immutable revision reviewed before a write."""

    model_config = ConfigDict(extra="forbid", strict=True)
    revision: int = Field(ge=0)


class SaveEnvironment(EnvironmentScripts):
    """Publish all scripts and explicit variable edits atomically."""

    name: str = Field(min_length=1, max_length=80)
    repo: str | None = Field(None, pattern=REPO_PATTERN, max_length=140)
    revision: int = Field(ge=0)
    variables: list[VariableEdit] = Field(default_factory=list, max_length=200, repr=False)


class GlobalInput(RevisionInput):
    """Modify personal variables without enabling global scripts."""

    variables: list[VariableEdit] = Field(default_factory=list, max_length=200, repr=False)


class CopyInput(RevisionInput):
    """Copy a reviewed environment to an entitled repository."""

    repo: str = Field(pattern=REPO_PATTERN, max_length=140)
    name: str = Field(min_length=1, max_length=80)


class RestoreInput(RevisionInput):
    """Restore one historical version as a new current revision."""

    source_revision: int = Field(gt=0)


class DefaultInput(RevisionInput):
    """A repository default choice; null selects the platform base."""

    environment_id: str | None = Field(None, max_length=80)


class ImportInput(RevisionInput):
    """An owner-reviewed dotenv batch, reparsed on commit."""

    environment_id: str | None = Field(None, max_length=80)
    global_revision: int | None = Field(None, ge=0)
    text: str = Field(max_length=MAX_IMPORT_BYTES, repr=False)
    ordinary_names: list[str] = Field(default_factory=list, max_length=100)


class TestInput(BaseModel):
    """Fixed inputs to a disposable preparation test."""

    model_config = ConfigDict(extra="forbid", strict=True)
    environment_id: str = Field(max_length=80)
    environment_revision: int = Field(ge=1)
    global_revision: int = Field(ge=0)
    ref: str | None = Field(None, max_length=255)


class SuggestInput(BaseModel):
    """Read lockfile facts without executing repository code."""

    model_config = ConfigDict(extra="forbid", strict=True)
    repo: str = Field(pattern=REPO_PATTERN, max_length=140)
    ref: str | None = Field(None, max_length=255)


class ClaimTestInput(BaseModel):
    """Host proof that this process understands the environment contract."""

    model_config = ConfigDict(extra="forbid", strict=True)
    capabilities: list[str] = Field(default_factory=list, max_length=100)
    attestation: dict[str, Any] = Field(default_factory=dict)


class TestUpdate(BaseModel):
    """Bounded execution results under a matching host lease."""

    model_config = ConfigDict(extra="forbid", strict=True)
    generation: int = Field(ge=1)
    state: Literal["running", "succeeded", "failed", "cancelled"] = "running"
    steps: list[dict[str, Any]] = Field(default_factory=list, max_length=3, repr=False)
    detail: str = Field(default="", max_length=1000, repr=False)
    image_digest: str | None = Field(None, max_length=512)
    cleanup_complete: bool = False
    #: The host's receipt for the installation snapshot this run saved.
    snapshot: dict[str, Any] | None = Field(None, repr=False)


def _snapshots(store: Any) -> Any:
    """The snapshot records, or ``None`` where this deployment's store has none."""
    return getattr(store, "environment_snapshots", None)


async def _entitled(store: Any, user_id: str, repo: str, credentials: Any) -> None:
    try:
        await require_entitled_repo(
            repo, user_id, store=store.source_control, app_credentials=credentials
        )
    except RepoNotAllowed:
        raise HTTPException(403, "Repository is not available to this owner") from None


async def _job_entitled(store: Any, user_id: str, job: dict[str, Any], credentials: Any) -> None:
    """Recheck the owner's access to a job's repository.

    A temporary workspace has no repository: ownership of the job, already
    checked by every caller, is the whole question.
    """
    if job["repo"] is not None:
        await _entitled(store, user_id, job["repo"], credentials)


@router.get("/environments")
async def list_environments(
    repo: str | None = Query(None, pattern=REPO_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """List owned repository configurations, their default selection and snapshot."""
    store = require_store(store)
    environments = await store.environments.list_environments(user["user_id"], repo)
    snapshots = _snapshots(store)
    snapshots_enabled = snapshots is not None and await snapshots.enabled()
    summaries = (
        await snapshots.summaries(
            user["user_id"],
            await store.environments.current_setup_fingerprints(
                user["user_id"], [e["id"] for e in environments]
            ),
        )
        if snapshots_enabled and environments
        else {}
    )
    return {
        "environments": [{**e, "snapshot": summaries.get(e["id"])} for e in environments],
        "globals": await store.environments.get_global_environment(user["user_id"]),
        "snapshots_enabled": snapshots_enabled,
    }


@router.get("/environments/{environment_id}")
async def read_environment(
    environment_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Read configuration with write-only Secret fields."""
    return await require_store(store).environments.get_environment(user["user_id"], environment_id)


async def _save(
    body: SaveEnvironment,
    environment_id: str | None,
    user: dict[str, Any],
    store: Any,
    credentials: Any,
) -> dict[str, Any]:
    if body.repo is None:
        raise HTTPException(422, "Choose a repository for this environment")
    await _entitled(store, user["user_id"], body.repo, credentials)
    return await store.environments.save_environment(
        user["user_id"],
        repo=body.repo,
        name=body.name,
        environment_id=environment_id,
        revision=body.revision,
        document=body.model_dump(
            mode="json",
            include={"setup_script", "start_script", "verify_script", "update_script", "actions"},
        ),
        edits=[v.model_dump() for v in body.variables],
    )


@router.post("/environments", status_code=201)
async def create_environment(
    body: SaveEnvironment,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Create a reusable environment before its first task runs."""
    return await _save(body, None, user, require_store(store), credentials)


@router.patch("/environments/{environment_id}")
async def save_environment(
    environment_id: str,
    body: SaveEnvironment,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Publish a new version without changing any bound workspace."""
    return await _save(body, environment_id, user, require_store(store), credentials)


@router.delete("/environments/{environment_id}", status_code=204)
async def delete_environment(
    environment_id: str,
    revision: int = Query(ge=1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> Response:
    """Archive an environment and clear a matching default."""
    store = require_store(store)
    await store.environments.archive_environment(user["user_id"], environment_id, revision)
    if (snapshots := _snapshots(store)) is not None:
        await snapshots.discard(user["user_id"], environment_id)
    return Response(status_code=204)


class RebuildInput(BaseModel):
    """Which branch's head to build the installation snapshot from."""

    model_config = ConfigDict(extra="forbid", strict=True)
    ref: str | None = Field(None, min_length=1, max_length=255)


@router.post("/environments/{environment_id}/snapshot", status_code=202)
async def rebuild_snapshot(
    environment_id: str,
    body: RebuildInput,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Queue a fresh installation snapshot of the environment as it is now."""
    store = require_store(store)
    snapshots = _snapshots(store)
    if snapshots is None or not await snapshots.enabled():
        raise HTTPException(409, "Snapshots need automatic sandbox snapshots to be enabled")
    binding_id, repo = await store.environments.bind_current_environment(
        user["user_id"], environment_id
    )
    await _entitled(store, user["user_id"], repo, credentials)
    from cloud_agent.environment_source import resolve_test_commit

    sha = await resolve_test_commit(repo, body.ref, credentials)
    await snapshots.discard(user["user_id"], environment_id)
    snapshot_id = await snapshots.queue_prebuild(
        user_id=user["user_id"], binding_id=binding_id, repo=repo, base_sha=sha, requested=True
    )
    if snapshot_id is None:
        raise HTTPException(409, "This environment has no installation script to snapshot")
    return {"snapshot_id": snapshot_id, "base_sha": sha}


@router.delete("/environments/{environment_id}/snapshot", status_code=204)
async def delete_snapshot(
    environment_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> Response:
    """Delete an environment's installation snapshots; new tasks install in full."""
    store = require_store(store)
    await store.environments.get_environment(user["user_id"], environment_id)
    if (snapshots := _snapshots(store)) is not None:
        await snapshots.discard(user["user_id"], environment_id)
    return Response(status_code=204)


@router.get("/environment-globals")
async def read_globals(
    user: dict[str, Any] = Depends(require_agent_owner), store: Any = Depends(get_storage)
) -> dict[str, Any]:
    """Read personally scoped global variable metadata."""
    return await require_store(store).environments.get_global_environment(user["user_id"])


@router.patch("/environment-globals")
async def save_globals(
    body: GlobalInput,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Publish global edits after checking every active merged environment."""
    return await require_store(store).environments.save_environment(
        user["user_id"],
        repo=None,
        name="Global",
        environment_id=None,
        revision=body.revision,
        document=EnvironmentScripts().model_dump(),
        edits=[v.model_dump() for v in body.variables],
    )


@router.put("/environment-default")
async def set_default(
    body: DefaultInput,
    repo: str = Query(pattern=REPO_PATTERN),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Set an owned environment as this repository's default."""
    store = require_store(store)
    await _entitled(store, user["user_id"], repo, credentials)
    return await store.environments.set_environment_default(
        user["user_id"], repo, body.environment_id, body.revision
    )


@router.delete("/environment-default")
async def clear_default(
    repo: str = Query(pattern=REPO_PATTERN),
    revision: int = Query(ge=0),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Clear an owner-reviewed default choice."""
    return await require_store(store).environments.set_environment_default(
        user["user_id"], repo, None, revision
    )


@router.post("/environments/{environment_id}/copy")
async def copy_environment(
    environment_id: str,
    body: CopyInput,
    idempotency_key: str | None = Header(None, max_length=128),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Copy scripts and encrypted variables within one owner."""
    store = require_store(store)
    await _entitled(store, user["user_id"], body.repo, credentials)
    return await store.environments.copy_environment(
        user["user_id"],
        environment_id,
        repo=body.repo,
        name=body.name,
        revision=body.revision,
        idempotency_key=idempotency_key,
    )


@router.get("/environments/{environment_id}/versions")
async def environment_versions(
    environment_id: str,
    before: int | None = Query(None, ge=1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Read paginated immutable history without secret values."""
    return {
        "versions": await require_store(store).environments.list_environment_versions(
            user["user_id"], environment_id, before
        )
    }


@router.post("/environments/{environment_id}/restore")
async def restore_environment(
    environment_id: str,
    body: RestoreInput,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Restore reviewed history as a new CAS-checked version."""
    return await require_store(store).environments.restore_environment(
        user["user_id"],
        environment_id,
        revision=body.revision,
        source_revision=body.source_revision,
    )


@router.get("/environment-globals/versions")
async def global_versions(
    before: int | None = Query(None, ge=1),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Read historical personal global variables."""
    repository = require_store(store).environments
    current = await repository.get_global_environment(user["user_id"])
    return {
        "versions": await repository.list_environment_versions(
            user["user_id"], current["id"], before
        )
        if current["id"]
        else []
    }


@router.post("/environment-globals/restore")
async def restore_globals(
    body: RestoreInput,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Restore a global version after validating all dependent environments."""
    repository = require_store(store).environments
    current = await repository.get_global_environment(user["user_id"])
    if not current["id"]:
        raise EnvironmentNotFound("Global configuration not found")
    return await repository.restore_environment(
        user["user_id"], current["id"], revision=body.revision, source_revision=body.source_revision
    )


@router.post("/environment-import/preview")
async def preview_import(
    body: ImportInput,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Parse input atomically without returning submitted values."""
    repository = require_store(store).environments
    current = (
        await repository.get_environment(user["user_id"], body.environment_id)
        if body.environment_id
        else await repository.get_global_environment(user["user_id"])
    )
    names = {v["name"] for v in current["variables"]}
    return {
        "revision": current["revision"],
        "variables": [
            {
                "name": v.name,
                "secret": v.secret,
                "secret_allowed": not v.value or len(v.value.strip()) >= MIN_SECRET_CHARS,
                "phase": v.phase,
                "operation": "replace" if v.name in names else "add",
            }
            for v in parse_env(body.text, ordinary_names=body.ordinary_names)
        ],
    }


@router.post("/environment-import")
async def import_variables(
    body: ImportInput,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Reparse and publish the complete batch at its reviewed revision."""
    repository = require_store(store).environments
    current = (
        await repository.get_environment(user["user_id"], body.environment_id)
        if body.environment_id
        else await repository.get_global_environment(user["user_id"])
    )
    if current["repo"]:
        await _entitled(store, user["user_id"], current["repo"], credentials)
    global_view = await repository.get_global_environment(user["user_id"])
    if body.global_revision is not None and global_view["revision"] != body.global_revision:
        raise EnvironmentConflict("Global variables changed; preview the import again")
    return await repository.save_environment(
        user["user_id"],
        repo=current["repo"],
        name=current["name"],
        environment_id=current["id"],
        revision=body.revision,
        document={
            k: current.get(k, "")
            for k in ("setup_script", "start_script", "verify_script", "update_script", "actions")
        },
        edits=[
            {**v.model_dump(exclude={"source"}), "operation": "replace"}
            for v in parse_env(body.text, ordinary_names=body.ordinary_names)
        ],
        expected_global_revision=body.global_revision,
    )


@router.post("/environment-runs", status_code=201)
async def test_environment(
    body: TestInput,
    idempotency_key: str | None = Header(None, max_length=128),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Queue a disposable test without minting any inference credentials."""
    store = require_store(store)
    selection = {
        "environment_id": body.environment_id,
        "environment_id_provided": True,
        "environment_revision": body.environment_revision,
        "global_revision": body.global_revision,
    }
    if idempotency_key:
        existing = await store.environments.find_environment_run(
            user["user_id"], idempotency_key, selection, body.ref
        )
        if existing is not None:
            return existing
    env = await store.environments.get_environment(user["user_id"], body.environment_id)
    repo = env["repo"]
    if not repo:
        raise HTTPException(422, "Choose a repository environment to test")
    await _entitled(store, user["user_id"], repo, credentials)
    from cloud_agent.environment_source import resolve_test_commit

    sha = await resolve_test_commit(repo, body.ref, credentials)
    return await store.environments.create_environment_run(
        user["user_id"],
        repo=repo,
        base_sha=sha,
        selection=selection,
        key=idempotency_key,
        ref=body.ref,
    )


@router.get("/environment-runs/{run_id}")
async def read_run(
    run_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Read progress and bounded redacted output for an owned test."""
    return await require_store(store).environments.get_environment_run(user["user_id"], run_id)


@router.post("/environment-runs/{run_id}/cancel")
async def cancel_test(
    run_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Cancel queued or active tests idempotently."""
    return await require_store(store).environments.cancel_environment_run(user["user_id"], run_id)


@router.get("/worker/environment-runs/orphans")
async def orphan_tests(
    host: dict[str, str] = Depends(verify_claiming_host), store: Any = Depends(get_storage)
) -> dict[str, Any]:
    """Name only this enrolled host's expired resources for cleanup."""
    return {"runs": await require_store(store).environments.list_environment_orphans(host["host"])}


@router.post("/worker/environment-runs/claim")
async def claim_environment_test(
    body: ClaimTestInput,
    host: dict[str, str] = Depends(verify_claiming_host),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
    gateway: Any = Depends(get_gateway_client),
) -> dict[str, Any] | None:
    """Hand only an environment-capable enrolled host a test or prebuild lease."""
    if ENVIRONMENT_CAPABILITY not in body.capabilities:
        return None
    store = require_store(store)
    snapshot_capable = ENVIRONMENT_SNAPSHOT_CAPABILITY in body.capabilities
    run = await store.environments.claim_environment_run(
        host["host"], host["credential_hash"], body.attestation, snapshot_capable=snapshot_capable
    )
    if run is None:
        return None
    try:
        await require_agent_access(gateway, run["user_id"])
        await _entitled(store, run["user_id"], run["repo"], credentials)
        bound = await store.environments.get_environment_binding(run["user_id"], run["binding_id"])
        from cloud_agent.environment_source import read_repository_token

        clone_token = await read_repository_token(run["repo"], credentials)
    except Exception:
        await store.environments.fail_environment_preflight(
            run["id"], host["host"], run["lease_generation"]
        )
        if (snapshots := _snapshots(store)) is not None:
            await snapshots.fail_capture(
                run_id=run["id"], detail="Repository access was unavailable"
            )
        raise HTTPException(
            503, "Environment credentials or repository access are unavailable"
        ) from None
    snapshots = _snapshots(store)
    snapshot = (
        await snapshots.capture_target(run, host["host"])
        if snapshot_capable and snapshots is not None
        else None
    )
    return {
        **run,
        "environment": environment_payload(bound, body.capabilities),
        "clone_token": clone_token,
        "snapshot": snapshot,
    }


@router.post("/worker/environment-runs/{run_id}")
async def report_test(
    run_id: str,
    body: TestUpdate,
    host: dict[str, str] = Depends(verify_claiming_host),
    store: Any = Depends(get_storage),
) -> dict[str, bool]:
    """Renew or finish only this host's currently fenced test run."""
    store = require_store(store)
    snapshots = _snapshots(store)
    if body.snapshot is not None:
        if snapshots is None:
            raise HTTPException(409, "Snapshot receipt does not match this run")
        # Recorded while the lease is still live, before the final update ends it.
        recorded = await snapshots.record_capture(
            run_id=run_id, host=host["host"], generation=body.generation, receipt=body.snapshot
        )
        if not recorded:
            raise HTTPException(409, "Snapshot receipt does not match this run")
    accepted = await store.environments.update_environment_run(
        run_id, host["host"], body.generation, body.model_dump(exclude={"generation", "snapshot"})
    )
    if not accepted:
        raise HTTPException(409, "Environment test was cancelled or its lease was lost")
    if body.state in {"failed", "cancelled"} and snapshots is not None:
        await snapshots.fail_capture(run_id=run_id, detail=body.detail or "Snapshot build failed")
    return {"accepted": True}


@router.get("/worker/environment-snapshots/deletions")
async def snapshot_deletions(
    host: dict[str, str] = Depends(verify_claiming_host), store: Any = Depends(get_storage)
) -> dict[str, Any]:
    """Snapshot objects to delete from S3, and local snapshot images to drop."""
    _ = host
    snapshots = _snapshots(require_store(store))
    return {"snapshots": await snapshots.deletions() if snapshots is not None else []}


@router.post("/worker/environment-snapshots/{snapshot_id}/deleted", status_code=204)
async def snapshot_deleted(
    snapshot_id: str,
    host: dict[str, str] = Depends(verify_claiming_host),
    store: Any = Depends(get_storage),
) -> Response:
    """Record that this host deleted a snapshot's stored object."""
    _ = host
    if (snapshots := _snapshots(require_store(store))) is not None:
        await snapshots.confirm_deleted(snapshot_id)
    return Response(status_code=204)


@router.post("/environment-script-suggestions")
async def suggest_script(
    body: SuggestInput,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Suggest reviewed lockfile commands without executing source code."""
    await _entitled(require_store(store), user["user_id"], body.repo, credentials)
    from cloud_agent.environment_source import script_suggestions

    return await script_suggestions(body.repo, body.ref, credentials)


@router.get("/jobs/{job_id}/thread/environment")
async def workspace_environment(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
) -> dict[str, Any]:
    """Show the workspace's fixed versions and preparation history."""
    from cloud_agent.api._owner_auth import _owned_job

    store = require_store(store)
    job = await _owned_job(store.conversation_queries, job_id, user)
    view = await store.environments.workspace_environment_view(
        user["user_id"], str(job["thread_id"])
    )
    if view.get("binding_id") and view.get("required"):
        from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

        broker = await sandbox_manager_for_job(store.host_pool, job_id)
        if broker is not None:
            try:
                receipt = await broker.environment_status(str(job["thread_id"]), view["binding_id"])
                if receipt.get("binding_id") == view["binding_id"]:
                    view["steps"] = await store.environments.redact_environment_steps(
                        user["user_id"], view["binding_id"], receipt.get("steps", [])
                    )
            except SandboxManagerError:
                pass
    return view


async def _action_context(
    user: dict[str, Any],
    store: Any,
    run: dict[str, Any],
    credentials: Any,
    *,
    require_ready: bool = True,
    recheck_entitlement: bool = True,
) -> tuple[dict[str, Any], Any, Any]:
    from cloud_agent.api.workspace import _terminal_owner_workspace

    job_id = run.get("job_id")
    if not job_id:
        raise HTTPException(409, "Action workspace has no current job")
    job, broker = await _terminal_owner_workspace(
        job_id=job_id,
        user=user,
        store=store,
        app_credentials=credentials,
        require_ready=require_ready,
        recheck_entitlement=recheck_entitlement,
    )
    bound = None
    if require_ready:
        bound = await store.environments.get_environment_binding(user["user_id"], run["binding_id"])
        await broker.prepare_environment(str(job["thread_id"]), bound)
    return job, broker, bound


@router.post("/jobs/{job_id}/thread/environment/actions/{action_id}/runs")
async def start_action(
    job_id: str,
    action_id: str,
    idempotency_key: str = Header(..., max_length=128),
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Execute a saved command through the retained owner's PTY mechanism."""
    from cloud_agent.api._owner_auth import _owned_job
    from cloud_agent.api.workspace import _terminal_owner_workspace

    store = require_store(store)
    job = await _owned_job(store.conversation_queries, job_id, user)
    if not job.get("environment_binding_id"):
        raise HTTPException(409, "This workspace has no reusable environment actions")
    run = await store.environments.create_environment_action_run(
        user["user_id"],
        str(job["thread_id"]),
        job["environment_binding_id"],
        action_id,
        idempotency_key,
    )
    if run["state"] != "starting":
        return run
    job, broker = await _terminal_owner_workspace(
        job_id=job_id, user=user, store=store, app_credentials=credentials, require_ready=True
    )
    bound = await store.environments.get_environment_binding(user["user_id"], run["binding_id"])
    terminal = await broker.create_terminal(
        str(job["thread_id"]),
        rows=24,
        cols=100,
        environment=bound,
        action_id=action_id,
        run_id=run["id"],
    )
    updated = await store.environments.update_environment_action_run(
        user["user_id"], run["id"], {"terminal_id": terminal["id"], **_action_result(terminal)}
    )
    if updated["terminal_id"] != terminal["id"]:
        # Stopped while the host was still preparing: the stop found no
        # terminal to end, and nothing tracks this one, so end it here.
        try:
            await broker.delete_terminal(str(job["thread_id"]), terminal["id"])
        except SandboxManagerError as exc:
            if exc.status_code != 404:
                raise
    return updated


def _action_result(terminal: dict[str, Any]) -> dict[str, Any]:
    state = terminal.get("state", "running")
    if state == "exited":
        state = "succeeded" if terminal.get("exit_code") == 0 else "failed"
    return {
        "state": state,
        "exit_code": terminal.get("exit_code"),
        "output": str(terminal.get("output") or ""),
    }


def _missing_action_result() -> dict[str, Any]:
    return {
        "state": "unknown",
        "exit_code": None,
        "output": "Execution result is unavailable. This run will not be replayed; review external effects before starting a new run.",
    }


@router.get("/environment-action-runs/{run_id}")
async def action_status(
    run_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Read live terminal state and bounded masked output for an operation."""
    store = require_store(store)
    run = await store.environments.get_environment_action_run(user["user_id"], run_id)
    if run["state"] in {"succeeded", "failed", "closed", "cancelled", "stopped", "unknown"}:
        return run
    job, broker, _bound = await _action_context(
        user, store, run, credentials, require_ready=False, recheck_entitlement=False
    )
    terminal_id = run["terminal_id"] or "term_" + run["id"]
    try:
        terminal = await broker.terminal_output(str(job["thread_id"]), terminal_id)
        update = _action_result(terminal)
    except SandboxManagerError as exc:
        if exc.status_code != 404:
            raise
        # A starting HTTP request can still be in flight. Do not fence it prematurely.
        if run["state"] == "starting" and run.get("updated_at"):
            from datetime import UTC, datetime

            if (datetime.now(UTC) - run["updated_at"]).total_seconds() < 1200:
                return run
        update = _missing_action_result()
    run = await store.environments.update_environment_action_run(user["user_id"], run_id, update)
    return {**run, "job_id": job["id"]}


@router.post("/environment-action-runs/{run_id}/stop")
async def stop_action(
    run_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Stop this operation's terminal without touching other group processes."""
    store = require_store(store)
    run = await store.environments.get_environment_action_run(user["user_id"], run_id)
    if run["state"] in {"succeeded", "failed", "closed", "cancelled", "stopped", "unknown"}:
        return run
    job, broker, _ = await _action_context(
        user, store, run, credentials, require_ready=False, recheck_entitlement=False
    )
    update: dict[str, Any] = {"state": "stopped"}
    terminal_id = run["terminal_id"] or "term_" + run["id"]
    try:
        output = await broker.terminal_output(str(job["thread_id"]), terminal_id)
        update["output"] = output.get("output", "")
        update["exit_code"] = output.get("exit_code")
        if output.get("state") in {"exited", "unknown", "failed"}:
            update = _action_result(output)
    except SandboxManagerError as exc:
        if exc.status_code != 404:
            raise
        update = _missing_action_result()
    try:
        await broker.delete_terminal(str(job["thread_id"]), terminal_id)
    except SandboxManagerError as exc:
        if exc.status_code != 404:
            raise
    return await store.environments.update_environment_action_run(user["user_id"], run_id, update)


@router.post("/jobs/{job_id}/thread/environment/retry")
async def retry_preparation(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Retry failed preparation using the same workspace binding."""
    from cloud_agent.api._owner_auth import _owned_job
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

    store = require_store(store)
    job = await _owned_job(store.conversation_queries, job_id, user)
    await _job_entitled(store, user["user_id"], job, credentials)
    if not job.get("environment_binding_id"):
        raise HTTPException(409, "Workspace has no environment binding")
    # Preparation runs inside the container, starting it if stopped.
    await fence_workspace_start(store, job)
    broker = await sandbox_manager_for_job(store.host_pool, job_id)
    if broker is None:
        raise HTTPException(503, "Workspace host is unavailable")
    bound = await store.environments.get_environment_binding(
        user["user_id"], job["environment_binding_id"]
    )
    receipt = await broker.prepare_environment(str(job["thread_id"]), bound)
    await store.environments.record_environment_preparation(
        user["user_id"], str(job["thread_id"]), receipt
    )
    return receipt


class RuntimeRefreshInput(BaseModel):
    """Compare-and-swap the current runtime selection."""

    model_config = ConfigDict(extra="forbid")
    runtime_revision: int = Field(ge=0)


@router.post("/jobs/{job_id}/thread/environment/runtime")
async def refresh_runtime_environment(
    job_id: str,
    body: RuntimeRefreshInput,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Select latest runtime variables explicitly for subsequent turns."""
    from cloud_agent.api._owner_auth import _owned_job

    store = require_store(store)
    job = await _owned_job(store.conversation_queries, job_id, user)
    await _job_entitled(store, user["user_id"], job, credentials)
    if not job.get("environment_binding_id"):
        raise HTTPException(409, "Workspace has no environment binding")
    return await store.environments.refresh_runtime_environment(
        user["user_id"], job["environment_binding_id"], body.runtime_revision
    )


@router.post("/jobs/{job_id}/thread/environment/skip-start")
async def skip_environment_start(
    job_id: str,
    user: dict[str, Any] = Depends(require_agent_owner),
    store: Any = Depends(get_storage),
    credentials: Any = Depends(get_agent_app_credentials),
) -> dict[str, Any]:
    """Allow one next turn to proceed after an explicitly acknowledged start failure."""
    from cloud_agent.api._owner_auth import _owned_job
    from cloud_agent.sandbox_manager_routing import sandbox_manager_for_job

    store = require_store(store)
    job = await _owned_job(store.conversation_queries, job_id, user)
    await _job_entitled(store, user["user_id"], job, credentials)
    if not job.get("environment_binding_id"):
        raise HTTPException(409, "Workspace has no environment binding")
    broker = await sandbox_manager_for_job(store.host_pool, job_id)
    if broker is None:
        raise HTTPException(503, "Workspace host is unavailable")
    receipt = await broker.environment_status(str(job["thread_id"]), job["environment_binding_id"])
    if not receipt.get("setup_done") or not any(
        step.get("phase") == "start" and step.get("state") == "failed"
        for step in receipt.get("steps", [])
    ):
        raise HTTPException(
            409, "Only a failed startup after successful installation may be skipped"
        )
    await store.environments.request_skip_environment_start(
        user["user_id"], job["environment_binding_id"], job_id
    )
    return {"skip_start_next_turn": True}
