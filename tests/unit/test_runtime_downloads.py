"""A host downloads an updated agent CLI, checks it, and copies it into a chat.

Docker is faked at ``subprocess.run``; what is pinned is the order of steps and
what each step is allowed — which one has a network, which one runs the
packages' scripts, and that only a version that passed its check is kept where
hosts look for it. ``tests/external`` runs the same steps against real Docker.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host import runtime_images, runtime_install
from cloud_agent_host.runtime_downloads import RuntimeDownloads, _Job
from cloud_agent_host.runtime_images import (
    by_runtime,
    discover,
    image_ref,
    repository,
    short_id,
)
from cloud_agent_host.runtime_probe import ProbeResult
from cloud_agent_shared.errors import SandboxError

NAMESPACE = "freeinference-cloud-agent-sandbox-staging"
IMAGE = "freeinference-cloud-agent-job:staging"
BASE = "sha256:" + "ab12" * 16
OLD_BASE = "sha256:" + "cd34" * 16
NONCE = "0123456789abcdef0123456789abcdef"
BEFORE = "2026-10-15T00:00:00Z"


def _target(version: str = "2.1.300", nonce: str = NONCE, runtime: str = "claude-code"):
    return {"runtime": runtime, "version": version, "before": BEFORE, "nonce": nonce}


class FakeDocker:
    """Just enough of the Docker CLI for one install, recording every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.images: dict[str, str] = {IMAGE: BASE}
        self.fetch_exit = 0
        self.fetch_output = "added 2 packages in 2s"
        self.scripts_output = "2.1.300 (Claude Code)\n"
        self.scripts_stderr = ""
        self.scripts_exit = 0
        #: Refuse to create a container from a committed image, as a full disk does.
        self.refuse_committed_create = False
        #: Every image ``commit`` made, tagged or not, in order.
        self.committed: list[str] = []
        self.containers: dict[str, list[str]] = {}
        self.leftover_containers: list[str] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        args = argv[1:]
        self.calls.append(args)
        verb = args[0]
        if args[:2] == ["image", "inspect"]:
            found = self.images.get(args[2])
            return self._done(found or "", 0 if found else 1)
        if args[:2] == ["image", "ls"]:
            if args[2] == "--filter":
                refs = [ref for ref in self.images if ref.startswith(repository(NAMESPACE) + ":")]
            else:
                refs = [ref for ref in self.images if ref.startswith(args[2] + ":")]
            return self._done("\n".join(refs))
        if verb == "create":
            if self.refuse_committed_create and any(arg in self.committed for arg in args):
                return self._done("", 1, "Error response from daemon: no space left on device")
            container = f"c{len(self.containers)}"
            self.containers[container] = args
            return self._done(container)
        if verb == "start":
            created = self.containers[args[-1]]
            if "none" not in created:
                return self._done(self.fetch_output, self.fetch_exit)
            return self._done(self.scripts_output, self.scripts_exit, self.scripts_stderr)
        if verb == "commit":
            ref = args[-1] if ":" in args[-1] else None
            image = "sha256:" + f"{len(self.calls):064x}"
            self.committed.append(image)
            if ref:
                self.images[ref] = image
            return self._done(image)
        if verb == "tag":
            self.images[args[2]] = self.images[args[1]]
            return self._done("")
        if verb == "rmi":
            self.images.pop(args[1], None)
            return self._done("")
        if verb == "ps":
            return self._done("\n".join(self.leftover_containers))
        return self._done("")

    @staticmethod
    def _done(stdout: str, code: int = 0, stderr: str = "") -> SimpleNamespace:
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)

    def ran(self, verb: str) -> list[list[str]]:
        return [call for call in self.calls if call[0] == verb]


class _Reader:
    """``docker cp <container>:<path> -``, streaming a tree that always arrives."""

    def __init__(self, argv: list[str], **_kwargs: Any) -> None:
        self.argv = argv
        self.stdout = SimpleNamespace(close=lambda: None)

    def wait(self, timeout: float | None = None) -> int:
        return 0


@pytest.fixture
def docker(monkeypatch) -> FakeDocker:
    fake = FakeDocker()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr(subprocess, "Popen", _Reader)
    return fake


class Probe:
    def __init__(self, result: ProbeResult = ProbeResult(True)) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> ProbeResult:
        self.calls.append(kwargs)
        return self.result


def _downloads(probe: Probe | None = None, **kwargs: Any) -> RuntimeDownloads:
    return RuntimeDownloads(
        docker="docker",
        image=IMAGE,
        namespace=NAMESPACE,
        image_menu=lambda: {"claude-code": ["2.1.220"], "codex": ["0.145.0"]},
        probe=probe or Probe(),
        **kwargs,
    )


def _run_once(downloads: RuntimeDownloads) -> _Job | None:
    """One pass of the worker loop, on this thread."""
    job = downloads._next(BASE)
    if job is not None:
        downloads._attempted[(job.target.runtime, job.target.version)] = (
            job.target.nonce,
            job.base_id,
        )
        downloads._download(job)
    return job


def _reported(
    downloads: RuntimeDownloads, version: str = "2.1.300", runtime: str = "claude-code"
) -> dict[str, Any]:
    """This host's report on one target."""
    (report,) = (
        report
        for report in downloads.report()
        if (report["runtime"], report["version"]) == (runtime, version)
    )
    return report


# ── the install ───────────────────────────────────────────────────────────


def test_only_npm_has_a_network_and_only_a_checked_version_is_kept(docker):
    probe = Probe()
    changes: list[dict[str, Any]] = []
    downloads = _downloads(probe)
    downloads._on_change = lambda: changes.append(_reported(downloads))
    downloads.apply([_target()])

    assert _run_once(downloads) is not None

    fetch, scripts, clean = (call for call in docker.ran("create"))
    # Step 1: the network, and npm with every package script off.
    assert fetch[fetch.index("--network") + 1] == "bridge"
    assert "--ignore-scripts" in fetch[fetch.index("-c") + 1]
    for step in (fetch, scripts):
        assert step[step.index("--cap-drop") + 1] == "ALL"
        assert "no-new-privileges" in step
    assert fetch[-3:] == [
        "/opt/runtimes/claude-code/2.1.300",
        BEFORE,
        "@anthropic-ai/claude-code@2.1.300",
    ]
    # Step 2: the packages' own scripts, with no network at all…
    assert scripts[scripts.index("--network") + 1] == "none"
    assert scripts[scripts.index("-c") + 1].startswith("set -e; npm rebuild")
    # …and the finished tree, alone, onto a fresh copy of the sandbox image.
    assert clean[clean.index("--network") + 1] == "none"
    assert clean[-1] == IMAGE
    assert ["cp", "-", "c2:/opt/runtimes/claude-code"] in docker.calls
    # Step 3: the check runs on the candidate, which hosts do not look at…
    (checked,) = probe.calls
    candidate = checked["image"]
    assert candidate.startswith(f"{repository(NAMESPACE)}-candidate:")
    assert (checked["runtime_id"], checked["version"]) == ("claude-code", "2.1.300")
    # …and only then is it kept where they do.
    final = image_ref(NAMESPACE, "claude-code", "2.1.300", BASE)
    assert ["tag", candidate, final] in docker.calls
    assert candidate not in docker.images
    assert [image.version for image in discover("docker", NAMESPACE)] == ["2.1.300"]
    commit = next(call for call in docker.ran("commit") if call[-1] == candidate)
    assert f"LABEL {runtime_images.NAMESPACE_LABEL}={NAMESPACE}" in commit
    # Progress only moves forward, and finishes at ready.
    assert [change["progress"] for change in changes] == [5, 55, 75, 100]
    assert changes[-1]["state"] == "ready"
    # Every work container is removed.
    removed = {call[-1] for call in docker.ran("rm")}
    assert set(docker.containers) <= removed


def test_a_failed_fetch_is_reported_and_not_retried_until_asked_again(docker):
    docker.fetch_exit = 1
    docker.fetch_output = "npm error code ECONNRESET\nnpm error network aborted"
    downloads = _downloads()
    downloads.apply([_target()])

    _run_once(downloads)

    report = _reported(downloads)
    assert report["state"] == "failed"
    assert report["error"].startswith("npm could not download claude-code 2.1.300:")
    assert "ECONNRESET" in report["error"]
    assert downloads._next(BASE) is None
    # The admin pressing Try again sends a new nonce.
    downloads.apply([_target(nonce="f" * 32)])
    assert downloads._next(BASE) is not None


def test_a_scripts_step_docker_refuses_still_removes_the_fetched_tree(docker):
    """The fetch is committed as an untagged image holding the whole tree again.

    Nothing but this download ever removes it, so a scripts container Docker
    will not create, as on a full disk, must not strand it, nor one more for
    every Try again.
    """
    docker.refuse_committed_create = True
    downloads = _downloads()
    downloads.apply([_target()])

    _run_once(downloads)

    report = _reported(downloads)
    assert report["state"] == "failed"
    assert report["error"].startswith("could not create an install container:")
    (fetched,) = docker.committed
    assert ["rmi", fetched] in docker.calls


def test_a_binary_reporting_another_version_is_not_kept(docker):
    docker.scripts_output = "2.1.299 (Claude Code)\n"
    probe = Probe()
    downloads = _downloads(probe)
    downloads.apply([_target()])

    _run_once(downloads)

    assert _reported(downloads)["error"].startswith(
        "claude-code 2.1.300 reports a different version"
    )
    assert probe.calls == []
    assert discover("docker", NAMESPACE) == []


def test_a_binary_that_reports_its_version_on_stderr_is_kept(docker):
    """pi before 0.84.0 prints ``--version`` to stderr, after npm's line on stdout."""
    docker.scripts_output = "rebuilt dependencies successfully\n"
    docker.scripts_stderr = "2.1.300\n"
    probe = Probe()
    downloads = _downloads(probe)
    downloads.apply([_target()])

    _run_once(downloads)

    assert _reported(downloads)["state"] == "ready"
    assert len(probe.calls) == 1


def test_a_version_that_fails_its_check_is_removed(docker):
    probe = Probe(
        ProbeResult(False, "claude-code 2.1.300 completed a turn without the model's answer")
    )
    downloads = _downloads(probe)
    downloads.apply([_target()])

    _run_once(downloads)

    assert downloads.report() == [
        {
            "runtime": "claude-code",
            "version": "2.1.300",
            "nonce": NONCE,
            "state": "failed",
            "progress": 0,
            "error": "claude-code 2.1.300 completed a turn without the model's answer",
        }
    ]
    assert docker.ran("tag") == []
    assert discover("docker", NAMESPACE) == []


def test_a_version_the_image_already_holds_is_ready_at_once(docker):
    downloads = _downloads()
    downloads.apply([_target("2.1.220")])

    assert _run_once(downloads) is None
    assert _reported(downloads, "2.1.220")["state"] == "ready"
    assert docker.ran("create") == []


def test_a_new_sandbox_image_downloads_and_checks_its_targets_again(docker):
    """The check was against the old image's wrappers; the old build then goes."""
    old = image_ref(NAMESPACE, "claude-code", "2.1.300", OLD_BASE)
    docker.images[old] = "sha256:" + "ee" * 32
    downloads = _downloads()
    downloads.apply([_target()])

    assert _run_once(downloads) is not None

    assert old not in docker.images
    assert [image.base for image in discover("docker", NAMESPACE)] == [short_id(BASE)]


def test_kilo_and_unknown_runtimes_are_never_targets(docker):
    downloads = _downloads()
    downloads.apply([_target(runtime="kilo"), _target(runtime="emacs"), {"runtime": "codex"}])
    assert downloads._targets == {}


def test_a_withdrawn_or_replaced_target_takes_its_report_with_it(docker):
    downloads = _downloads()
    downloads.apply([_target()])
    _run_once(downloads)
    assert _reported(downloads)["state"] == "ready"

    downloads.apply([_target("2.1.301", nonce="e" * 32)])
    assert downloads.report() == []
    downloads.apply([])
    assert downloads._targets == {}


def test_the_version_in_use_is_held_beside_the_update_in_the_order_sent(docker):
    """A host that joins mid-update must still run what every other host runs."""
    docker.scripts_output = "2.1.235 (Claude Code)\n"
    downloads = _downloads()
    held = _target("2.1.235", nonce="a" * 32)
    downloads.apply([held, _target(), _target("2.1.299", nonce="b" * 32)])
    # Two per runtime: a third is not a control plane of this release.
    assert sorted(version for _, version in downloads._targets) == ["2.1.235", "2.1.300"]

    first = _run_once(downloads)
    assert first is not None and first.target.version == "2.1.235"
    docker.scripts_output = "2.1.300 (Claude Code)\n"
    second = _run_once(downloads)
    assert second is not None and second.target.version == "2.1.300"
    assert _run_once(downloads) is None
    assert {(report["version"], report["state"]) for report in downloads.report()} == {
        ("2.1.235", "ready"),
        ("2.1.300", "ready"),
    }
    assert sorted(image.version for image in discover("docker", NAMESPACE)) == [
        "2.1.235",
        "2.1.300",
    ]

    # The update switched new chats: the held version is no longer sent, and
    # its report goes, but the image stays for any chat or rollback that needs it.
    downloads.apply([_target()])
    assert [report["version"] for report in downloads.report()] == ["2.1.300"]
    assert "2.1.235" in {image.version for image in discover("docker", NAMESPACE)}


def test_a_restarted_runner_removes_what_a_stopped_download_left(docker):
    docker.leftover_containers = ["dead1", "dead2"]
    docker.images[f"{repository(NAMESPACE)}-candidate:claude-code_2.1.300_{short_id(BASE)}"] = "x"

    _downloads()._remove_leftovers()

    assert ["rm", "-f", "dead1"] in docker.calls and ["rm", "-f", "dead2"] in docker.calls
    assert not any("-candidate:" in ref for ref in docker.images)


def test_with_nothing_to_hold_the_worker_only_cleans_up(docker):
    """Every host runs the worker; one never asked to download leaves Docker alone."""
    downloads = _downloads()
    downloads.start()
    downloads.stop()

    assert downloads._thread is not None and not downloads._thread.is_alive()
    asked = {tuple(call[:2]) for call in docker.calls if isinstance(call, list)}
    assert asked == {("ps", "--all"), ("image", "ls")}


# ── naming the images ─────────────────────────────────────────────────────


def test_image_names_are_this_deployments_and_parse_back():
    assert repository("Free Inference/Staging") == "free-inference-staging-agent-runtime"
    ref = image_ref(NAMESPACE, "claude-code", "2.1.300-beta.1", BASE)
    assert ref == f"{NAMESPACE}-agent-runtime:claude-code_2.1.300-beta.1_{'ab12' * 3}"
    assert runtime_images._parse(ref, NAMESPACE) is not None
    assert runtime_images._parse(ref, "another-deployment") is None
    assert (
        runtime_images._parse(f"{NAMESPACE}-agent-runtime:kilo_7.4.17_{'ab12' * 3}", NAMESPACE)
        is None
    )
    assert runtime_images._parse(f"{NAMESPACE}-agent-runtime:<none>", NAMESPACE) is None


def test_downloaded_versions_are_listed_oldest_first():
    images = [
        runtime_images.RuntimeImage("codex", version, "ab12ab12ab12", "ref")
        for version in ("0.162.1", "0.150.0", "0.162.1", "0.162.0-alpha.1")
    ]
    assert by_runtime(images) == {"codex": ["0.150.0", "0.162.0-alpha.1", "0.162.1"]}


# ── copying a downloaded version into a chat ──────────────────────────────


class FakeManager:
    """The retained-sandbox manager's surface the copy uses."""

    def __init__(self, *, marker_present: bool = False, activate_exit: int = 0) -> None:
        self.backend = SimpleNamespace(
            docker_binary="docker",
            image=IMAGE,
            sandbox_namespace=NAMESPACE,
            image_menu=lambda: {"claude-code": ["2.1.220"]},
        )
        self.marker_present = marker_present
        self.activate_exit = activate_exit
        self.execs: list[tuple[list[str], int | None]] = []
        self.runs: list[list[str]] = []
        self.copies: list[list[str]] = []

    def exec_capture(self, _spec, argv, *, user_uid=None, **_kwargs):
        self.execs.append((argv, user_uid))
        if argv[0] == "/bin/sh":
            return SimpleNamespace(returncode=0 if self.marker_present else 1, stderr=b"")
        if argv[0] == "/usr/bin/python3":
            return SimpleNamespace(returncode=self.activate_exit, stderr=b"file is not protected")
        return SimpleNamespace(returncode=0, stderr=b"")

    def _require(self, _spec):
        return SimpleNamespace(container_id="chat-container")

    def _run(self, argv, *, timeout):
        self.runs.append(argv)
        return SimpleNamespace(returncode=0, stdout="source-container\n", stderr="")

    def _run_bytes(self, argv, *, timeout, stdin=None, input_bytes=None):
        self.copies.append(argv)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")


@pytest.fixture
def downloaded(monkeypatch):
    """This host holds claude-code 2.1.300, and ``docker cp`` reads succeed."""
    images = {image_ref(NAMESPACE, "claude-code", "2.1.300", BASE): "x", IMAGE: BASE}
    fake = FakeDocker()
    fake.images = images
    monkeypatch.setattr(subprocess, "run", fake)

    class Reader:
        def __init__(self, argv, **_kwargs):
            self.argv = argv
            self.stdout = SimpleNamespace(close=lambda: None)

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(runtime_install.subprocess, "Popen", Reader)
    return fake


def test_a_version_the_image_holds_is_not_copied(downloaded):
    manager = FakeManager()
    runtime_install.ensure_downloaded_runtime(manager, None, "claude-code", "2.1.220")
    assert manager.execs == []


def test_a_copy_already_in_the_chat_is_not_made_again(downloaded):
    manager = FakeManager(marker_present=True)
    runtime_install.ensure_downloaded_runtime(manager, None, "claude-code", "2.1.300")
    assert manager.copies == []


def test_a_downloaded_version_is_copied_in_as_root_and_activated(downloaded):
    manager = FakeManager()

    runtime_install.ensure_downloaded_runtime(manager, None, "claude-code", "2.1.300")

    source = manager.runs[0]
    assert source[:4] == ["docker", "create", "--network", "none"]
    assert source[-1] == image_ref(NAMESPACE, "claude-code", "2.1.300", BASE)
    (copy,) = manager.copies
    assert copy[:3] == ["docker", "cp", "-"]
    assert copy[3].startswith("chat-container:/opt/runtimes/claude-code/.install-")
    activate = next(argv for argv, _uid in manager.execs if argv[0] == "/usr/bin/python3")
    assert activate[-3:] == ["/opt/runtimes/claude-code/2.1.300", "2.1.300", runtime_install.MARKER]
    # Every step inside the chat runs as root, never as a member.
    assert {uid for _argv, uid in manager.execs} == {0}
    # The stage and the source container go either way.
    assert manager.execs[-1][0][:2] == ["rm", "-rf"]
    assert manager.runs[-1] == ["docker", "rm", "source-container"]


def test_a_tree_that_is_not_root_owned_is_refused(downloaded):
    manager = FakeManager(activate_exit=1)
    with pytest.raises(SandboxError, match=r"could not activate claude-code 2\.1\.300"):
        runtime_install.ensure_downloaded_runtime(manager, None, "claude-code", "2.1.300")
    assert manager.runs[-1] == ["docker", "rm", "source-container"]


def test_a_version_this_host_never_downloaded_says_how_to_fix_it(downloaded):
    with pytest.raises(SandboxError, match="update the agent again"):
        runtime_install.ensure_downloaded_runtime(FakeManager(), None, "claude-code", "2.1.301")


def test_the_activation_refuses_a_tree_root_does_not_own_outright(tmp_path):
    """The script itself: a file a member could change is never activated."""
    stage = tmp_path / "stage"
    (stage / "2.1.300" / "bin").mkdir(parents=True)
    tool = stage / "2.1.300" / "bin" / "claude"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o777)
    target = tmp_path / "runtimes" / "2.1.300"
    target.parent.mkdir()
    result = subprocess.run(
        [
            "python3",
            "-I",
            "-c",
            runtime_install._ACTIVATE,
            str(stage),
            str(target),
            "2.1.300",
            runtime_install.MARKER,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    # Owned by the test runner's user and writable by anyone: both refuse it.
    assert "not protected" in result.stderr
    assert not target.exists()
