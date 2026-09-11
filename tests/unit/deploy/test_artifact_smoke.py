"""Every artifact has a start check, and the check knows where it runs.

The table in `deploy/release/smoke.py` is the one place that says what "this
artifact starts" means, which also makes it the one place that can quietly lose
an artifact: a role missing from it is a role whose images are published without
having been run, and the manifest would still look complete. These tests hold it
against the release contract and against the sandbox image's own labels, and
check the identity assertions without a Docker daemon.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from deploy.release import release_manifest as rm, smoke

REPO_ROOT = Path(__file__).resolve().parents[3]
SANDBOX_DOCKERFILE = REPO_ROOT / "deploy/sandbox/Dockerfile.agent-sandbox"

COMMIT = "4af1b771ac8f736a1be24f20cb307012af114a18"


def test_every_role_a_release_carries_has_a_start_check() -> None:
    assert sorted(smoke.ROLES) == sorted(rm.EXPECTED_PLATFORMS), (
        "a role in the release contract without a start check is an image that reaches a "
        "release having never been run"
    )


def test_web_is_checked_once_per_variant() -> None:
    assert smoke.ROLES["web"].variants == ("root", "agents")
    assert set(smoke.ROLES["web"].variants) == set(rm.WEB_VARIANTS.values())


@pytest.mark.parametrize("role", sorted(smoke.ROLES))
def test_each_check_runs_on_the_architecture_the_artifact_deploys_to(role: str) -> None:
    """A start on the wrong architecture proves the wrong image.

    This is the same rule the build matrix follows, stated once more where the
    start happens: the control plane and the console are deployed on ARM64, the
    host artifacts on x86_64, and starting one where it will never run says
    nothing about where it will.
    """
    platforms = rm.EXPECTED_PLATFORMS[role]
    assert len(platforms) == 1, f"{role} is expected on {platforms}"
    assert smoke.ROLES[role].arch == platforms[0], (
        f"{role} is started on {smoke.ROLES[role].arch} and deployed on {platforms[0]}"
    )


def test_every_runtime_the_sandbox_labels_is_verified() -> None:
    """No runtime may be labelled into a release without a command that proves it.

    The manifest publishes these labels as `runtime_catalog` and the host stamps
    attempts from them, so a label nobody checks is a version a release claims
    and never verifies.
    """
    labelled = set(
        re.findall(
            rf"{re.escape(rm.RUNTIME_LABEL_PREFIX)}([a-z0-9-]+){re.escape(rm.RUNTIME_LABEL_SUFFIX)}=",
            SANDBOX_DOCKERFILE.read_text(),
        )
    )
    assert labelled, "the sandbox Dockerfile declares no runtime labels"

    unverified = sorted(labelled - set(smoke.RUNTIME_COMMANDS))
    assert not unverified, (
        f"these runtimes are labelled but have no command in RUNTIME_COMMANDS: {unverified}"
    )
    stale = sorted(set(smoke.RUNTIME_COMMANDS) - labelled)
    assert not stale, f"RUNTIME_COMMANDS names runtimes the image does not install: {stale}"


# ------------------------------------------------------------ identity labels


def matching_labels(**overrides: str) -> dict[str, str]:
    labels = {
        "org.opencontainers.image.source": (
            "https://github.com/HarvardMadSys/hybridInference-cloud-agent"
        ),
        "org.opencontainers.image.revision": COMMIT,
        "io.hybridinference.artifact.role": "web",
        "io.hybridinference.artifact.base_path": "/agents",
    }
    labels.update(overrides)
    return labels


def test_the_identity_labels_are_what_the_release_will_be_checked_against(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(smoke, "image_labels", lambda image: matching_labels())
    evidence = smoke.verify_identity("image", "web", "agents", COMMIT)
    assert any("revision" in line for line in evidence)
    assert any("base path" in line for line in evidence)


@pytest.mark.parametrize(
    ("role", "variant", "revision", "labels"),
    [
        ("control", None, COMMIT, matching_labels()),
        ("web", "root", COMMIT, matching_labels()),
        ("web", "agents", "b" * 40, matching_labels()),
        (
            "web",
            "agents",
            COMMIT,
            matching_labels(**{"org.opencontainers.image.revision": "unknown"}),
        ),
        (
            "web",
            "agents",
            COMMIT,
            matching_labels(**{"io.hybridinference.artifact.role": "runner"}),
        ),
        (
            "web",
            "agents",
            COMMIT,
            matching_labels(**{"io.hybridinference.artifact.base_path": ""}),
        ),
    ],
)
def test_a_wrong_identity_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    role: str,
    variant: str | None,
    revision: str,
    labels: dict[str, str],
) -> None:
    """An image that cannot name itself is refused before it is ever started.

    `unknown` is the default the Dockerfiles carry, so it is the shape a missing
    build argument takes — and a release full of `unknown` revisions is one no
    resolver accepts, discovered there rather than here.
    """
    monkeypatch.setattr(smoke, "image_labels", lambda image: labels)
    with pytest.raises(smoke.SmokeFailure):
        smoke.verify_identity("image", role, variant, revision)


def test_an_unknown_role_is_refused_before_anything_is_started() -> None:
    with pytest.raises(smoke.SmokeFailure) as raised:
        smoke.verify("migrate", None, "image", "suffix", keep=True)
    assert "unknown role" in str(raised.value)


# -------------------------------------------------------------------- the CLI


def test_list_answers_what_the_roles_are_without_an_image(capsys: pytest.CaptureFixture) -> None:
    assert smoke.main(["--list"]) == 0
    output = capsys.readouterr().out
    for role in smoke.ROLES:
        assert role in output
    assert "does not prove" in output


def test_a_role_and_an_image_are_required() -> None:
    with pytest.raises(SystemExit):
        smoke.main([])


class Recorder:
    """The order events happened in, which is the whole assertion below."""

    def __init__(self) -> None:
        self.events: list[str] = []


def test_the_control_check_stops_containers_before_it_removes_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Order is invisible in the happy path and wrong in the first draft.

    Removing a network that still has endpoints attached fails, and the failure
    is ignored because there is nothing useful to do about it — so the wrong
    order leaves the network on the runner rather than failing anything. The
    first version returned from inside the network context, which removed the
    network first; this records the order the real function produces with
    in-memory doubles, so the next reordering fails here instead of on a host.
    """
    recorder = Recorder()

    class FakeNetwork:
        def __init__(self, name: str) -> None:
            self.name = name

        def create(self) -> None:
            recorder.events.append("network.create")

        def remove(self) -> None:
            recorder.events.append("network.remove")

    class FakeContainer:
        def __init__(self, name: str, run: str) -> None:
            self.name = name
            self.run = run
            self.published = {8000: 1234}

        @classmethod
        def for_run(cls, kind: str, run: str) -> FakeContainer:
            return cls(name=f"smoke-{kind}-{run}", run=run)

        def start(self, image: str, **kwargs: object) -> None:
            recorder.events.append(f"start:{self.name}")

        def wait(self, timeout: int = 0) -> None:
            recorder.events.append(f"wait:{self.name}")

        def exit_code(self) -> int:
            return 0

        def logs(self) -> str:
            return "database system is ready to accept connections"

        def url(self, port: int, path: str = "/") -> str:
            return f"http://127.0.0.1:{self.published[port]}{path}"

        def stop(self) -> None:
            recorder.events.append(f"stop:{self.name}")

    monkeypatch.setattr(smoke, "Network", FakeNetwork)
    monkeypatch.setattr(smoke, "Container", FakeContainer)
    monkeypatch.setattr(smoke, "wait_for_http", lambda url, **kwargs: (200, ""))
    monkeypatch.setattr(smoke, "_control_environment", lambda dsn: {})
    monkeypatch.setattr(smoke, "_drop_image", lambda image: recorder.events.append("drop-image"))

    assert smoke.verify_control("control:ci-1", suffix="1", keep=True)

    lifelines = [
        event for event in recorder.events if event.startswith(("start:", "stop:", "network."))
    ]
    assert lifelines == [
        "network.create",
        "start:smoke-control-pg-1",
        "start:smoke-control-migrate-1",
        "start:smoke-control-app-1",
        "stop:smoke-control-app-1",
        "stop:smoke-control-migrate-1",
        "stop:smoke-control-pg-1",
        "network.remove",
    ], lifelines
