"""Choosing a CLI version: what gets picked, and what actually runs.

Three seams, each of which fails silently if it is wrong, which is why each is
pinned here rather than left to the end-to-end run:

* **Resolution order** decides which version an attempt is stamped with. Get it
  wrong in favour of the admin's selection and every in-flight thread may lose
  access to the native session files in its retained container.
* **Label parsing** decides what the pool believes each host can run. Degrade a
  missing menu to *empty* rather than to "just the default" and every stored
  selection becomes unservable at once.
* **PATH selection** decides what the sandbox executes. This is the one that
  cannot be allowed to fall back: the binary name still resolves without the
  versioned directory — to the image default — so a job would run one version
  while its attempt row and audit trail record another.
"""

from __future__ import annotations

from typing import Any

from cloud_agent.domain.runtimes import resolve_runtime_version
from cloud_agent.schemas import WorkerClaimAttestation, WorkerClaimResponse
from cloud_agent_host.runner import (
    ClaimedJob,
    _claim_attestation,
    runtime_version_unavailable,
)
from cloud_agent_host.sandbox import (
    ContainerBackend,
    _labelled,
    _split_menu,
)
from cloud_agent_shared.environment import BoundEnvironment
from cloud_agent_shared.environment_snapshots import EnvironmentSnapshotRef, snapshot_object_key
from cloud_agent_shared.ssh_access import SshAccess

_MENU = {"claude-code": ["2.1.220", "2.1.235"]}
_DEFAULT = {"claude-code": "2.1.220"}


# ── Which version the claim stamps ───────────────────────────────────────


def test_the_host_default_applies_when_nothing_is_selected() -> None:
    """The behaviour every deployment has until an admin chooses."""
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version=None,
            selected={},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.220"
    )


def test_a_selection_the_host_offers_wins_over_its_default() -> None:
    """The feature."""
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version=None,
            selected={"claude-code": "2.1.235"},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.235"
    )


def test_a_thread_keeps_the_version_it_started_on() -> None:
    """An admin switching versions must not reach into live conversations.

    Honouring the new selection here would make one admin click point a live
    conversation at a different CLI build than the one that owns its native
    session files.
    """
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version="2.1.220",
            selected={"claude-code": "2.1.235"},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.220"
    )


def test_a_selection_this_host_cannot_serve_falls_back() -> None:
    """An image rebuilt with a shorter menu must not park the queue.

    The selection outliving the version is a real sequence — rebuild the image
    without an extra, and the stored choice names something no longer installed.
    Running the default is a smaller wrong than refusing every claim.
    """
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version=None,
            selected={"claude-code": "9.9.9"},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.220"
    )


def test_a_runtime_nobody_reported_stamps_unknown() -> None:
    """ "Unknown" is a truthful record; an invented version is not."""
    assert (
        resolve_runtime_version(
            runtime="codex",
            thread_version=None,
            selected={},
            available=_MENU,
            host_default=_DEFAULT,
        )
        is None
    )


def test_a_selection_for_another_runtime_is_not_applied() -> None:
    """Selections are per runtime; a codex choice says nothing about claude."""
    assert (
        resolve_runtime_version(
            runtime="claude-code",
            thread_version=None,
            selected={"codex": "0.145.0"},
            available=_MENU,
            host_default=_DEFAULT,
        )
        == "2.1.220"
    )


# ── What the pool believes each host holds ───────────────────────────────


def test_the_menu_and_the_default_come_from_different_labels() -> None:
    """``.version`` is not a suffix of ``.versions``, so the two never mix."""
    labels = {
        "org.freeinference.runtime.claude-code.version": "2.1.220",
        "org.freeinference.runtime.claude-code.versions": "2.1.220,2.1.235",
    }
    assert _labelled(labels, ".version") == {"claude-code": "2.1.220"}
    assert _labelled(labels, ".versions") == {"claude-code": "2.1.220,2.1.235"}


def test_an_image_with_no_menu_label_offers_its_default() -> None:
    """Built before menus existed — it holds one version, and offers it.

    Degrading to an empty list instead would read as "this host can run
    nothing" and make every stored selection unservable at once.
    """
    assert _split_menu(None, "2.1.220") == ["2.1.220"]
    assert _split_menu("", "2.1.220") == ["2.1.220"]


def test_a_menu_missing_its_own_default_still_offers_it() -> None:
    """The default is what an unselected job runs; it is always available."""
    assert _split_menu("2.1.235", "2.1.220") == ["2.1.220", "2.1.235"]


def test_the_menu_keeps_the_image_order() -> None:
    """First is the default — a meaning, not an accident, so it is not sorted."""
    assert _split_menu("2.1.220, 2.1.235 ,", "2.1.220") == ["2.1.220", "2.1.235"]


# ── What the sandbox actually executes ───────────────────────────────────


class _Backend:
    """Only what version selection reads off a sandbox backend."""

    def __init__(self, available: dict[str, list[str]] | None) -> None:
        self._available = available

    def version_stamp(self) -> dict[str, Any]:
        return {"runtime_versions_available": self._available}


def _job(version: str | None) -> ClaimedJob:
    return ClaimedJob(
        job_id="ajob_1",
        thread_id="athr_1",
        attempt_id=1,
        attempt_no=1,
        repo="owner/name",
        base_sha=None,
        task_prompt="hi",
        runtime="claude-code",
        model="m",
        worker_token="w",
        sandbox_token="s",
        runtime_version=version,
    )


def test_the_reason_is_reportable_before_the_agent_starts() -> None:
    """``run_turn`` fails the attempt with this rather than letting it time out."""
    assert runtime_version_unavailable(_job("9.9.9"), _Backend(_MENU)) is not None
    assert runtime_version_unavailable(_job("2.1.235"), _Backend(_MENU)) is None


# ── The wire between them ────────────────────────────────────────────────


def test_the_claim_schema_declares_everything_the_runner_attests() -> None:
    """A key the model does not name is a key the model throws away.

    This is the general form of a bug that reached staging. The runner sends
    its whole ``version_stamp()`` as the claim attestation and ``claim_job``
    read ``runtime_versions_available`` out of it — but nothing declared that
    field on ``WorkerClaimAttestation``, so Pydantic dropped it in validation.
    The claim saw an empty menu, every stored selection failed its
    "does this host offer it?" check, and each one silently became a no-op.
    One log line was the only sign.

    Nothing caught it because every test went around the wire: the unit tests
    pass ``available=`` straight into ``resolve_runtime_version``, and the
    dbtests call ``store.claim_job(claim_attestation=...)`` directly. Both
    skip the model that was doing the dropping.

    So this asserts the join itself, and it fails for the *next* field too.
    """
    # Constructed rather than hand-built, so a key added to the real stamp is
    # a key this test sees. The docker binary does not exist, which takes the
    # degraded path — its keys are the ones a working inspect produces too, and
    # the keys are the whole subject here.
    backend = ContainerBackend(image="unused", docker_binary="/nonexistent-docker")
    reported = set(_claim_attestation(backend))
    declared = set(WorkerClaimAttestation.model_fields)

    dropped = reported - declared
    assert not dropped, (
        f"the runner attests {sorted(dropped)}, which WorkerClaimAttestation does "
        "not declare — Pydantic discards those keys and the claim never sees them"
    )


def test_the_menu_survives_validation() -> None:
    """The specific field, end to end through the model the endpoint applies."""
    attestation = WorkerClaimAttestation.model_validate(
        {
            "sandbox_runtime": "",
            "runtime_versions": {"claude-code": "2.1.220"},
            "runtime_versions_available": {"claude-code": ["2.1.220", "2.1.235"]},
            "helper_versions": {"claude-agent-sdk": "0.2.148"},
            "runner_capabilities": ["shared-sandbox-v1"],
        }
    )

    # `model_dump()` is what `api/worker.py` hands to `claim_job`, so it is the
    # value under test rather than the parsed object.
    assert attestation.model_dump()["runtime_versions_available"] == {
        "claude-code": ["2.1.220", "2.1.235"]
    }
    assert attestation.model_dump()["helper_versions"] == {"claude-agent-sdk": "0.2.148"}
    assert attestation.model_dump()["runner_capabilities"] == ["shared-sandbox-v1"]


class _ReadKeys(dict[str, Any]):
    """A claim body that records each key read from it, whether or not it is there."""

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__(body)
        self.read: set[str] = set()

    def __getitem__(self, key: str) -> Any:
        self.read.add(key)
        return super().__getitem__(key)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        self.read.add(key)
        return super().get(key, default)


def _claim(**optional: Any) -> WorkerClaimResponse:
    """A claim response with its required fields, and the optional ones given."""
    return WorkerClaimResponse(
        job_id="ajob_1",
        thread_id="athr_1",
        attempt_id=1,
        attempt_no=1,
        repo="owner/name",
        task_prompt="hi",
        runtime="claude-code",
        model="m",
        worker_token="w",
        **optional,
    )


def _full_claim() -> WorkerClaimResponse:
    """A claim response with every optional field filled in, each with a value the runner takes."""
    owner = "d" * 64
    snapshot = EnvironmentSnapshotRef.model_validate(
        {
            "snapshot_id": "5" * 32,
            "owner": owner,
            "object_key": snapshot_object_key(owner, "5" * 32),
            "sha256": "e" * 64,
            "size_bytes": 10,
            "base_image": "ghcr.io/harvardmadsys/cloud-agent-sandbox@sha256:" + "a" * 64,
            "source_sha": "c" * 40,
            "built_at": "2026-10-15T00:00:00Z",
        }
    )
    return _claim(
        thread_attachment_count=2,
        turn_attachment_count=1,
        environment=BoundEnvironment(binding_id="b").model_dump(mode="json"),
        environment_snapshot=snapshot.model_dump(mode="json"),
        ssh_access=SshAccess(revision=1),
        parent_job_id="ajob_0",
        turn_no=2,
        base_sha="c" * 40,
        setup_script="make deps",
        plugins=[{"plugin": "review@tools", "marketplace": "owner/tools"}],
        execution_profile="untrusted-kata-v1",
        reasoning_effort="high",
        runtime_version="2.1.235",
        thread_sandbox={
            "thread_id": "athr_1",
            "sandbox_id": "asbx_1",
            "generation": 1,
            "host": "host-a",
            "container_name": "cloud-agent-asbx_1",
            "runtime_id": "claude-code",
            "execution_profile": "untrusted-kata-v1",
            "state": "running",
            "context_status": "live",
            "activation_generation": 1,
            "process_generation": 1,
        },
        sandbox_token="s",
        mcp_token="t",
        clone_token="c",
        github_token="g",
        mcp_bindings=["docs"],
        metadata={"source": "test"},
    )


def test_the_claim_response_declares_everything_the_runner_reads() -> None:
    """The other direction of the same wire, and the same failure.

    ``ClaimedJob.from_response`` reads the claim body by key. A key the
    response model does not declare is a key the endpoint cannot send, so the
    runner reads ``None`` and carries on with its default — which for
    ``runtime_version`` meant the attempt row recorded 2.1.235 while the
    sandbox ran 2.1.220. The job succeeded. Nothing in its output disagreed
    with the row.

    Asserted as a join rather than a field list: this fails for whatever is
    threaded through next and forgotten in the middle. The runner reads some
    keys only when a section is there, so it is handed two bodies the response
    model produced, one with no optional field and one with every one. Each
    remembers every key the runner asks it for, present or not.
    """
    full = _full_claim()
    unfilled = [
        name
        for name, field in WorkerClaimResponse.model_fields.items()
        if getattr(full, name) == field.get_default(call_default_factory=True)
    ]
    assert not unfilled, (
        f"fill {unfilled} in `_full_claim`, or a key the runner reads only when "
        "they are there is never read here"
    )
    bare = _ReadKeys(_claim().model_dump(mode="json"))
    filled = _ReadKeys(full.model_dump(mode="json"))
    ClaimedJob.from_response(bare)
    ClaimedJob.from_response(filled)
    read = bare.read | filled.read
    declared = set(WorkerClaimResponse.model_fields)

    assert "runtime_version" in read
    unsendable = read - declared
    assert not unsendable, (
        f"ClaimedJob reads {sorted(unsendable)} from the claim body, which "
        "WorkerClaimResponse does not declare — the endpoint cannot send them "
        "and the runner will always see None"
    )
