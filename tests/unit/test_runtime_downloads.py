"""A host downloads an updated agent CLI, checks it, and copies it into a chat.

Docker is faked at ``subprocess.run``; what is pinned is the order of steps and
what each step is allowed — which one has a network, which one runs the
packages' scripts, and that only a version that passed its check is kept where
hosts look for it. ``tests/external`` runs the same steps against real Docker.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from types import SimpleNamespace
from typing import Any

import pytest

from cloud_agent_host import runtime_downloads, runtime_images, runtime_install
from cloud_agent_host.chat_sandbox_models import (
    CHAT_NAMESPACE_LABEL,
    CHAT_RUNTIME_LABEL,
    CHAT_RUNTIME_VERSION_LABEL,
)
from cloud_agent_host.runtime_downloads import RuntimeDownloads, _Job
from cloud_agent_host.runtime_images import (
    by_runtime,
    discover,
    image_ref,
    repository,
    short_id,
)
from cloud_agent_host.runtime_probe import ProbeResult
from cloud_agent_host.sandbox_checkpoints import checkpoint_image_reference
from cloud_agent_shared.errors import SandboxError

NAMESPACE = "freeinference-cloud-agent-sandbox-staging"
IMAGE = "freeinference-cloud-agent-job:staging"
BASE = "sha256:" + "ab12" * 16
OLD_BASE = "sha256:" + "cd34" * 16
NONCE = "0123456789abcdef0123456789abcdef"
BEFORE = "2026-10-15T00:00:00Z"
#: Where earlier releases kept this deployment's downloads: the namespace
#: folded, and so shared with every namespace that folds the same, as this
#: neighbour's does.
LEGACY = f"{NAMESPACE}-agent-runtime"
NEIGHBOUR = NAMESPACE.replace("-", "_")
#: Docker's grammar for a repository name of one path component.
DOCKER_REPOSITORY = re.compile(r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*")


def _target(version: str = "2.1.300", nonce: str = NONCE, runtime: str = "claude-code"):
    return {"runtime": runtime, "version": version, "before": BEFORE, "nonce": nonce}


def _kept_labels(version: str, namespace: str = NAMESPACE) -> dict[str, str]:
    """What a kept claude-code ``version``, and its candidate, are labelled (``_keep_tree``)."""
    return {
        runtime_images.WORK_LABEL: "",
        runtime_images.NAMESPACE_LABEL: namespace,
        runtime_images.RUNTIME_LABEL: "claude-code",
        runtime_images.VERSION_LABEL: version,
    }


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
        #: Labels by container and by image ID, inherited the way Docker does:
        #: a container takes its image's, under its own ``--label``; ``commit``
        #: takes the container's, under its ``--change LABEL``.
        self.labels: dict[str, dict[str, str]] = {}
        self.removed: set[str] = set()
        #: Refs ``rmi`` refuses, as Docker does an image a container uses…
        self.refuse_rmi: set[str] = set()
        #: …and refs it never answers for.
        self.hang_rmi: set[str] = set()
        #: What ``ps`` exits with; not 0 is a daemon that cannot answer.
        self.ps_exit = 0
        #: What listing this deployment's downloaded versions exits with.
        self.held_ls_exit = 0
        #: The daemon's clock, in seconds, and when each container was made,
        #: from which image: ``rmi`` refuses an image a container still uses,
        #: and ``container prune --filter until=`` goes by the age.
        self.now = 0.0
        self.created: dict[str, float] = {}
        self.made_from: dict[str, str] = {}

    def __call__(self, argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        args = argv[1:]
        self.calls.append(args)
        verb = args[0]
        if args[:3] == ["image", "inspect", "--format"]:
            # ``{{json .Config.Labels}}`` of each image named.
            return self._done("\n".join(json.dumps(self.labels.get(i)) for i in args[4:]))
        if args[:2] == ["image", "inspect"]:
            found = self.images.get(args[2])
            return self._done(found or "", 0 if found else 1)
        if args[:2] == ["image", "ls"]:
            wanted = self._wanted(args)
            if "--quiet" in args:
                found = {
                    image
                    for image in self.images.values()
                    if all(self._matches(self.labels.get(image, {}), label) for label in wanted)
                }
                return self._done("\n".join(sorted(found)))
            if args[2] == "--filter" and self.held_ls_exit:
                return self._done("", self.held_ls_exit, "Cannot connect to the Docker daemon")
            # Each tag of every image with the labels named, in the one
            # repository named if there is one: the label decides, not the name.
            named = None if args[2].startswith("--") else args[2]
            refs = [
                ref
                for ref, image in self.images.items()
                if named in (None, ref.rpartition(":")[0])
                and all(self._matches(self.labels.get(image, {}), label) for label in wanted)
            ]
            return self._done("\n".join(refs))
        if verb == "create":
            if self.refuse_committed_create and any(arg in self.committed for arg in args):
                return self._done("", 1, "Error response from daemon: no space left on device")
            container = f"c{len(self.containers)}"
            self.containers[container] = args
            image = args[args.index("--entrypoint") + 2]
            self.labels[container] = {
                **self.labels.get(self.images.get(image, image), {}),
                **self._pairs(args, "--label"),
            }
            self.created[container] = self.now
            self.made_from[container] = image
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
            self.labels[image] = {
                **self.labels.get(args[-2] if ref else args[-1], {}),
                **self._pairs(args, "--change", "LABEL "),
            }
            if ref:
                self.images[ref] = image
            return self._done(image)
        if verb == "tag":
            self.images[args[2]] = self.images[args[1]]
            return self._done("")
        if verb == "rmi":
            if args[1] in self.hang_rmi:
                raise subprocess.TimeoutExpired(argv, 120)
            in_use = any(
                self.made_from.get(container) == args[1]
                for container in self.containers
                if container not in self.removed
            )
            if in_use or args[1] in self.refuse_rmi:
                return self._done("", 1, "conflict: unable to remove repository reference")
            self.images.pop(args[1], None)
            return self._done("")
        if verb == "rm":
            self.removed.add(args[-1])
            return self._done("")
        if args[:2] == ["container", "prune"]:
            # Every container not running, which a never-started one is not,
            # with the labels named, made before ``until`` ago.
            wanted = self._wanted(args)
            (until,) = (arg.removeprefix("until=") for arg in args if arg.startswith("until="))
            age = sum(
                int(count) * {"h": 3600, "m": 60, "s": 1}[unit]
                for count, unit in re.findall(r"(\d+)([hms])", until)
            )
            pruned = [
                container
                for container in self.containers
                if container not in self.removed
                and self.created.get(container, 0.0) <= self.now - age
                and all(self._matches(self.labels[container], label) for label in wanted)
            ]
            self.removed.update(pruned)
            return self._done("Deleted Containers:\n" + "\n".join(pruned))
        if verb == "ps":
            if self.ps_exit:
                return self._done("", self.ps_exit, "Cannot connect to the Docker daemon")
            wanted = self._wanted(args)
            live = [
                container
                for container in self.containers
                if container not in self.removed
                and all(self._matches(self.labels[container], label) for label in wanted)
            ]
            if "--format" in args:
                template = args[args.index("--format") + 1]
                return self._done(
                    "\n".join(self._render(template, self.labels[container]) for container in live)
                )
            return self._done("\n".join([*self.leftover_containers, *live]))
        return self._done("")

    @staticmethod
    def _wanted(args: list[str]) -> list[str]:
        """The ``label=`` filters a call names."""
        return [arg.removeprefix("label=") for arg in args if arg.startswith("label=")]

    @staticmethod
    def _render(template: str, labels: dict[str, str]) -> str:
        """One ``ps --format`` line: each ``{{.Label "KEY"}}`` is that label's value."""
        return re.sub(
            r'\{\{\.Label "([^"]+)"\}\}', lambda found: labels.get(found[1], ""), template
        )

    @staticmethod
    def _pairs(args: list[str], flag: str, prefix: str = "") -> dict[str, str]:
        """``KEY=VALUE`` after each ``flag`` whose value starts with ``prefix``."""
        pairs: dict[str, str] = {}
        for at, arg in enumerate(args[:-1]):
            if arg == flag and args[at + 1].startswith(prefix):
                key, _, value = args[at + 1].removeprefix(prefix).partition("=")
                pairs[key] = value
        return pairs

    @staticmethod
    def _matches(labels: dict[str, str], wanted: str) -> bool:
        """Docker's ``label=`` filter: ``KEY`` is present, ``KEY=VALUE`` equal."""
        key, equals, value = wanted.partition("=")
        return key in labels and (not equals or labels[key] == value)

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


def _downloads(
    probe: Probe | None = None, namespace: str = NAMESPACE, **kwargs: Any
) -> RuntimeDownloads:
    return RuntimeDownloads(
        docker="docker",
        image=IMAGE,
        namespace=namespace,
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
    docker.labels[docker.images[old]] = _kept_labels("2.1.300")
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
    # its report goes, but the image stays for any chat that may still need
    # it. It goes only once nothing has needed it for a while (see below).
    downloads.apply([_target()])
    assert [report["version"] for report in downloads.report()] == ["2.1.300"]
    assert "2.1.235" in {image.version for image in discover("docker", NAMESPACE)}


def test_a_restarted_runner_removes_what_a_stopped_download_left(docker):
    docker.leftover_containers = ["dead1", "dead2"]
    docker.images[f"{repository(NAMESPACE)}-candidate:claude-code_2.1.300_{short_id(BASE)}"] = "x"
    docker.labels["x"] = _kept_labels("2.1.300")

    _downloads()._remove_leftovers()

    assert ["rm", "-f", "dead1"] in docker.calls and ["rm", "-f", "dead2"] in docker.calls
    assert not any("-candidate:" in ref for ref in docker.images)


def test_a_container_made_from_a_kept_version_is_not_install_work(docker):
    """``docker commit`` keeps a container's labels; the kept version drops this one.

    The install containers are marked so a restarted runner removes what a
    stopped download left. A chat copies the CLI out of a container made from
    the kept image (``runtime_install``): marked too, it would be removed by a
    runner restarting during the copy, failing the chat's start.
    """
    downloads = _downloads()
    downloads.apply([_target()])
    _run_once(downloads)
    final = image_ref(NAMESPACE, "claude-code", "2.1.300", BASE)
    # Docker cannot unset a label; an empty one is what the filter skips.
    assert docker.labels[docker.images[final]][runtime_images.WORK_LABEL] == ""
    source = docker(["docker", "create", "--network", "none", "--entrypoint", "/bin/true", final])

    downloads._remove_leftovers()

    assert ["rm", "-f", source.stdout] not in docker.calls


def test_with_nothing_to_hold_the_worker_only_cleans_up(docker):
    """Every host runs the worker; one never asked to download leaves Docker alone."""
    downloads = _downloads()
    downloads.start()
    downloads.stop()

    assert downloads._thread is not None and not downloads._thread.is_alive()
    asked = {tuple(call[:2]) for call in docker.calls if isinstance(call, list)}
    assert asked == {("ps", "--all"), ("image", "ls")}


# ── removing what nothing needs ───────────────────────────────────────────


class Clock:
    """``time.monotonic`` for the worker, moved by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _held(docker: FakeDocker, version: str, base: str = BASE) -> str:
    """This host holds claude-code ``version``, built on sandbox image ``base``."""
    ref = image_ref(NAMESPACE, "claude-code", version, base)
    docker.images[ref] = "sha256:" + f"{len(docker.images):064x}"
    docker.labels[docker.images[ref]] = _kept_labels(version)
    return ref


def _chat_labels(version: str, namespace: str = NAMESPACE) -> dict[str, str]:
    """What a retained chat container on claude-code ``version`` is labelled."""
    return {
        CHAT_NAMESPACE_LABEL: namespace,
        CHAT_RUNTIME_LABEL: "claude-code",
        CHAT_RUNTIME_VERSION_LABEL: version,
    }


def _chat(docker: FakeDocker, version: str, namespace: str = NAMESPACE) -> str:
    """A chat container on ``version``; ``ps --all`` lists it in any state."""
    container = f"chat{len(docker.containers)}"
    docker.containers[container] = []
    docker.labels[container] = _chat_labels(version, namespace)
    return container


def _cleaned_up(downloads: RuntimeDownloads, clock: Clock) -> None:
    """Two passes, the grace period apart: the first only notes what is unneeded."""
    downloads._clean_up()
    clock.now += runtime_downloads.UNNEEDED_GRACE_S
    downloads._clean_up()


def _held_versions() -> list[str]:
    return sorted(image.version for image in discover("docker", NAMESPACE))


def test_cleanup_keeps_targets_and_what_chats_run_and_removes_the_rest(docker):
    clock = Clock()
    downloads = _downloads(clock=clock)
    for version in ("2.1.200", "2.1.235", "2.1.240", "2.1.250", "2.1.300"):
        _held(docker, version)
    # A version a chat was using when a new sandbox image came: its build on
    # the old image stays after that chat is gone, and must go too.
    _held(docker, "2.1.210", OLD_BASE)
    _chat(docker, "2.1.235")
    # A checkpoint image carries its chat's labels (``docker commit`` keeps them).
    checkpoint = checkpoint_image_reference("asbx_moved", "c" * 32)
    docker.images[checkpoint] = "sha256:" + "c4" * 32
    docker.labels[docker.images[checkpoint]] = _chat_labels("2.1.240")
    # Another deployment sharing this Docker daemon never runs this one's downloads.
    _chat(docker, "2.1.250", namespace="another-deployment")
    downloads.apply([_target()])

    _cleaned_up(downloads, clock)

    assert _held_versions() == ["2.1.235", "2.1.240", "2.1.300"]
    # Never forced: Docker refuses, and so keeps, an image a container still uses.
    assert sorted(call[1].split("_")[1] for call in docker.ran("rmi")) == [
        "2.1.200",
        "2.1.210",
        "2.1.250",
    ]
    assert all(len(call) == 2 for call in docker.ran("rmi"))


def test_a_version_goes_only_after_nothing_has_needed_it_for_a_while(docker):
    """A switch can replace the version a new chat was just claimed on.

    That chat holds the version only once its container exists, and loading
    an environment snapshot can make that a while after the claim.
    """
    clock = Clock()
    downloads = _downloads(clock=clock)
    previous = _held(docker, "2.1.235")
    _held(docker, "2.1.300")
    downloads.apply([_target("2.1.235", nonce="a" * 32), _target()])
    downloads._clean_up()

    # The switch: the answer names only the update now.
    downloads.apply([_target()])
    downloads._clean_up()
    clock.now += runtime_downloads.UNNEEDED_GRACE_S - 1
    downloads._clean_up()
    assert previous in docker.images

    # The chat claimed just before the switch has made its container.
    chat = _chat(docker, "2.1.235")
    clock.now += runtime_downloads.UNNEEDED_GRACE_S
    downloads._clean_up()
    assert previous in docker.images

    # Deleted, the chat takes nothing with it until a whole grace period passes.
    docker.removed.add(chat)
    downloads._clean_up()
    clock.now += runtime_downloads.UNNEEDED_GRACE_S - 1
    downloads._clean_up()
    assert previous in docker.images
    clock.now += 1
    downloads._clean_up()
    assert previous not in docker.images
    assert _held_versions() == ["2.1.300"]


def test_a_version_an_answer_names_while_cleanup_runs_is_kept(docker, monkeypatch):
    """An admin may select a version this host still offers while a cleanup decides.

    The control plane accepts it because this host offers it, so a new chat
    may be claimed on it before the next beat says it went.
    """
    clock = Clock()
    downloads = _downloads(clock=clock)
    selected = _held(docker, "2.1.200")
    downloads.apply([_target()])
    downloads._clean_up()
    clock.now += runtime_downloads.UNNEEDED_GRACE_S
    read_chats = runtime_downloads.chat_versions

    def answered_meanwhile(*args: Any, **kwargs: Any) -> Any:
        # The answer naming the selection lands after this pass read the list.
        downloads.apply([_target("2.1.200", nonce="b" * 32), _target()])
        return read_chats(*args, **kwargs)

    monkeypatch.setattr(runtime_downloads, "chat_versions", answered_meanwhile)
    downloads._clean_up()

    assert selected in docker.images
    assert docker.ran("rmi") == []


@pytest.mark.parametrize("named_during", ["a_cleanup", "a_download"])
def test_a_version_named_again_waits_a_whole_grace_period_once_dropped(
    docker, monkeypatch, named_during
):
    """A chat claimed on it while an answer named it may not run it yet.

    The answer naming it can land while a cleanup decides, or while the worker
    downloads another version and runs no cleanup at all.
    """
    clock = Clock()
    downloads = _downloads(clock=clock)
    selected = _held(docker, "2.1.200")
    downloads.apply([_target()])
    downloads._clean_up()
    clock.now += runtime_downloads.UNNEEDED_GRACE_S
    naming = [_target("2.1.200", nonce="b" * 32), _target()]
    if named_during == "a_cleanup":
        read_chats = runtime_downloads.chat_versions

        def answered_meanwhile(*args: Any, **kwargs: Any) -> Any:
            downloads.apply(naming)
            return read_chats(*args, **kwargs)

        monkeypatch.setattr(runtime_downloads, "chat_versions", answered_meanwhile)
        downloads._clean_up()
        monkeypatch.setattr(runtime_downloads, "chat_versions", read_chats)
    else:
        downloads.apply(naming)
    # A later answer drops it again before the next cleanup.
    downloads.apply([_target()])

    downloads._clean_up()
    assert selected in docker.images
    clock.now += runtime_downloads.UNNEEDED_GRACE_S
    downloads._clean_up()
    assert selected not in docker.images


def test_nothing_is_removed_until_an_answer_says_what_to_hold(docker):
    """An answer without targets comes from a control plane that does not send them."""
    clock = Clock()
    downloads = _downloads(clock=clock)
    stale = _held(docker, "2.1.200")

    _cleaned_up(downloads, clock)
    assert stale in docker.images

    downloads.apply([_target()])
    downloads._clean_up()
    downloads.apply(None)
    clock.now += runtime_downloads.UNNEEDED_GRACE_S
    downloads._clean_up()
    assert stale in docker.images
    # It still holds what it was asked to; it only stops removing.
    assert list(downloads._targets) == [("claude-code", "2.1.300")]

    # Removing starts over: the time before is not counted.
    downloads.apply([_target()])
    downloads._clean_up()
    assert stale in docker.images
    clock.now += runtime_downloads.UNNEEDED_GRACE_S
    downloads._clean_up()
    assert stale not in docker.images


def test_a_target_this_host_cannot_use_still_keeps_its_version(docker):
    """A control plane from another release may send more, or another shape."""
    clock = Clock()
    downloads = _downloads(clock=clock)
    for version in ("2.1.200", "2.1.298", "2.1.299"):
        _held(docker, version)
    downloads.apply(
        [
            _target("2.1.235", nonce="a" * 32),
            _target(),
            # More than this release holds for one runtime…
            _target("2.1.299", nonce="b" * 32),
            # …and a shape it cannot read.
            {"runtime": "claude-code", "version": "2.1.298"},
        ]
    )

    _cleaned_up(downloads, clock)

    assert _held_versions() == ["2.1.298", "2.1.299"]


def test_a_menu_too_long_to_report_keeps_every_version_an_answer_names(docker):
    """The version stamp keeps these first (``ContainerBackend.version_stamp``)."""
    downloads = _downloads()
    downloads.apply([_target(), {"runtime": "claude-code", "version": "2.1.298"}])
    assert downloads.targets() == {("claude-code", "2.1.300"), ("claude-code", "2.1.298")}

    # An answer without targets leaves the ones this host holds.
    downloads.apply(None)
    assert downloads.targets() == {("claude-code", "2.1.300")}


def test_a_version_docker_will_not_remove_stays_and_the_rest_go(docker):
    """Docker refuses an image a container uses, such as a chat's copy source."""
    clock = Clock()
    downloads = _downloads(clock=clock)
    copying = _held(docker, "2.1.200")
    stale = _held(docker, "2.1.210")
    docker.refuse_rmi = {copying}
    downloads.apply([])

    _cleaned_up(downloads, clock)

    assert _held_versions() == ["2.1.200"]
    assert stale not in docker.images


def test_nothing_is_removed_when_docker_cannot_say_what_chats_run(docker):
    clock = Clock()
    downloads = _downloads(clock=clock)
    stale = _held(docker, "2.1.200")
    docker.ps_exit = 1
    downloads.apply([])

    _cleaned_up(downloads, clock)

    assert stale in docker.images
    assert docker.ran("rmi") == []


def _wait_for(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)


def test_the_worker_removes_what_nothing_needs_once_it_has_downloaded(docker, monkeypatch):
    monkeypatch.setattr(runtime_downloads, "UNNEEDED_GRACE_S", 0.0, raising=False)
    stale = _held(docker, "2.1.200")
    # Whether the stale version was still there each time the beat was nudged.
    nudged: list[bool] = []
    downloads = _downloads(on_change=lambda: nudged.append(stale in docker.images))
    downloads.apply([_target()])

    downloads.start()
    try:
        _wait_for(lambda: stale not in docker.images)
    finally:
        downloads.stop()

    assert _held_versions() == ["2.1.300"]
    assert _reported(downloads)["state"] == "ready"
    # Removing it nudges the beat too, so the menu is read again now.
    assert nudged[-1] is False


def test_a_failed_cleanup_never_stops_the_worker(docker, monkeypatch, caplog):
    monkeypatch.setattr(runtime_downloads, "UNNEEDED_GRACE_S", 0.0, raising=False)
    stale = _held(docker, "2.1.200")
    docker.hang_rmi = {stale}
    downloads = _downloads()
    downloads.apply([_target(), _target("0.162.1", runtime="codex")])

    with caplog.at_level(logging.ERROR):
        downloads.start()
        try:
            _wait_for(lambda: len(downloads.report()) == 2)
        finally:
            downloads.stop()

    # The cleanup after the first download hung on Docker; the next one still ran.
    assert ["rmi", stale] in docker.calls
    assert {report["runtime"] for report in downloads.report()} == {"claude-code", "codex"}
    assert "runtime download cleanup failed" in caplog.text


def test_a_version_a_chat_still_runs_goes_once_that_chat_is_deleted(docker, monkeypatch):
    """No answer comes when a chat is deleted, and the host may have nothing to download.

    Every update was rolled back to the image default, so the answers name
    nothing, and the one chat still on a downloaded version is deleted later.
    The worker has to look again by itself.
    """
    monkeypatch.setattr(runtime_downloads, "UNNEEDED_GRACE_S", 0.0, raising=False)
    monkeypatch.setattr(runtime_downloads, "_POLL_S", 0.01)
    withdrawn = _held(docker, "2.1.200")
    chat = _chat(docker, "2.1.200")
    downloads = _downloads()
    downloads.apply([])

    def passes() -> int:
        return sum(1 for call in docker.calls if call[:2] == ["container", "prune"])

    downloads.start()
    try:
        # It has looked twice, and kept the version the chat runs both times.
        _wait_for(lambda: passes() >= 2)
        assert withdrawn in docker.images
        docker.removed.add(chat)
        _wait_for(lambda: withdrawn not in docker.images)
    finally:
        downloads.stop()

    assert _held_versions() == []


def test_a_failed_listing_of_the_held_versions_is_tried_again(docker, monkeypatch):
    """A Docker that cannot list images has not said that this host holds none.

    The answers name nothing, so nothing but a cleanup still owed makes the
    worker look again.
    """
    monkeypatch.setattr(runtime_downloads, "UNNEEDED_GRACE_S", 0.0, raising=False)
    monkeypatch.setattr(runtime_downloads, "_POLL_S", 0.01)
    withdrawn = _held(docker, "2.1.200")
    docker.held_ls_exit = 1
    downloads = _downloads()
    downloads.apply([])

    def passes() -> int:
        return sum(1 for call in docker.calls if call[:2] == ["container", "prune"])

    downloads.start()
    try:
        # It keeps looking while Docker cannot say, and removes nothing.
        _wait_for(lambda: passes() >= 3)
        assert passes() >= 3
        assert withdrawn in docker.images
        docker.held_ls_exit = 0
        _wait_for(lambda: withdrawn not in docker.images)
    finally:
        downloads.stop()

    assert _held_versions() == []


# ── naming the images ─────────────────────────────────────────────────────


def test_image_names_are_this_deployments_and_parse_back():
    assert repository("Free Inference/Staging") == (
        "free-inference-staging-agent-runtime-d1ad2a5251b3"
    )
    ref = image_ref(NAMESPACE, "claude-code", "2.1.300-beta.1", BASE)
    assert ref == f"{NAMESPACE}-agent-runtime-74d1c3b2b18b:claude-code_2.1.300-beta.1_{'ab12' * 3}"
    assert runtime_images._parse(ref, NAMESPACE) is not None
    assert runtime_images._parse(ref, "another-deployment") is None
    assert (
        runtime_images._parse(f"{repository(NAMESPACE)}:kilo_7.4.17_{'ab12' * 3}", NAMESPACE)
        is None
    )
    assert runtime_images._parse(f"{repository(NAMESPACE)}:<none>", NAMESPACE) is None
    # What earlier releases kept parses back too; a candidate never does.
    tag = f"claude-code_2.1.300_{'ab12' * 3}"
    assert runtime_images._parse(f"{LEGACY}:{tag}", NAMESPACE) is not None
    assert runtime_images._parse(f"{repository(NAMESPACE)}-candidate:{tag}", NAMESPACE) is None
    assert runtime_images._parse(f"{LEGACY}-candidate:{tag}", NAMESPACE) is None


def test_namespaces_that_read_the_same_get_repositories_of_their_own():
    """Folding keeps neither case nor punctuation, so ``foo_bar`` and ``foo-bar`` read the same."""
    assert repository("foo_bar") == "foo-bar-agent-runtime-4928cae8b37b"
    assert repository("foo-bar") == "foo-bar-agent-runtime-7d89c4f517e3"
    assert image_ref("foo_bar", "claude-code", "2.1.300", BASE) != image_ref(
        "foo-bar", "claude-code", "2.1.300", BASE
    )
    # Still valid Docker repository names, the candidate's included.
    for namespace in ("Free Inference/Staging", "foo_bar", "--", "Ünïcode", "x" * 128):
        for name in (repository(namespace), f"{repository(namespace)}-candidate"):
            assert len(name) <= 255 and DOCKER_REPOSITORY.fullmatch(name), name


def test_deployments_whose_namespaces_read_the_same_keep_their_downloads_apart(docker):
    """Two deployments on one Docker daemon download the same version on the same image.

    Sharing a repository, the second download moved the tag off the first
    one's image: the version dropped out of that deployment's label-filtered
    discovery, and its menu, until it downloaded the version again and moved
    the tag back.
    """
    for namespace in (NAMESPACE, NEIGHBOUR):
        downloads = _downloads(namespace=namespace)
        downloads.apply([_target()])
        assert _run_once(downloads) is not None

    for namespace in (NAMESPACE, NEIGHBOUR):
        assert runtime_images.DownloadedVersions("docker", namespace).versions() == {
            "claude-code": ["2.1.300"]
        }
    (ours,) = discover("docker", NAMESPACE)
    (theirs,) = discover("docker", NEIGHBOUR)
    assert docker.images[ours.ref] != docker.images[theirs.ref]


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
    fake.labels["x"] = _kept_labels("2.1.300")
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


def test_a_runner_restarting_mid_copy_leaves_the_copy_alone(downloaded):
    """A version kept by an earlier release carries the install-work label.

    Its source container would inherit it, and a runner restarting while a
    chat copies the CLI removes every container that has it.
    """
    final = image_ref(NAMESPACE, "claude-code", "2.1.300", BASE)
    downloaded.labels[downloaded.images[final]][runtime_images.WORK_LABEL] = NAMESPACE

    class Manager(FakeManager):
        """Creates the source container through Docker."""

        def _run(self, argv, *, timeout):
            self.runs.append(argv)
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )

        def _run_bytes(self, argv, *, timeout, stdin=None, input_bytes=None):
            # The runner restarts while the files stream into the chat.
            _downloads()._remove_leftovers()
            return super()._run_bytes(argv, timeout=timeout, stdin=stdin)

    manager = Manager()
    runtime_install.ensure_downloaded_runtime(manager, None, "claude-code", "2.1.300")

    (source,) = downloaded.containers
    assert ["rm", "-f", source] not in downloaded.calls
    assert len(manager.copies) == 1


def _leak_a_copy_source(docker: FakeDocker, version: str) -> str:
    """Copy ``version`` into a chat whose process dies before removing its source.

    The source container is made exactly as the copy makes it; only the
    ``docker rm`` at the end never reaches Docker.
    """

    class Dying(FakeManager):
        def _run(self, argv, *, timeout):
            self.runs.append(argv)
            if argv[1] == "rm":
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout, check=False
            )

    runtime_install.ensure_downloaded_runtime(Dying(), None, "claude-code", version)
    (source,) = (c for c in docker.containers if c not in docker.removed)
    return source


def test_a_copy_source_left_behind_goes_once_it_is_old_and_its_version_with_it(docker):
    """The leaked source pins its version's image: Docker refuses to remove it."""
    clock = Clock()
    downloads = _downloads(clock=clock)
    held = _held(docker, "2.1.200")
    source = _leak_a_copy_source(docker, "2.1.200")
    downloads.apply([])

    _cleaned_up(downloads, clock)
    assert held in docker.images

    # An hour on, no copy can still be reading it.
    docker.now += 3601
    downloads._clean_up()

    assert source in docker.removed
    assert held not in docker.images


def test_a_young_copy_source_is_left_alone(docker):
    """A copy may still be reading it; only an old one is left behind for good."""
    clock = Clock()
    downloads = _downloads(clock=clock)
    held = _held(docker, "2.1.200")
    source = _leak_a_copy_source(docker, "2.1.200")
    docker.now += 3599
    downloads.apply([])

    _cleaned_up(downloads, clock)

    assert source not in docker.removed
    assert held in docker.images
    # Still owed, so a host with nothing to download tries again.
    assert downloads._cleanup_due
    # Only this deployment's copy sources, and only those an hour old.
    pruned = {tuple(call) for call in docker.calls if call[:2] == ["container", "prune"]}
    assert pruned == {
        (
            "container",
            "prune",
            "--force",
            "--filter",
            f"label={runtime_images.SOURCE_LABEL}={NAMESPACE}",
            "--filter",
            "until=1h",
        )
    }


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


# ── what earlier releases kept ────────────────────────────────────────────


def _kept_earlier(docker: FakeDocker, ref: str, version: str, namespace: str = NAMESPACE) -> str:
    """``ref`` is claude-code ``version`` as an earlier release kept it for ``namespace``."""
    docker.images[ref] = "sha256:" + f"{len(docker.images):064x}"
    docker.labels[docker.images[ref]] = _kept_labels(version, namespace)
    return ref


def test_a_version_an_earlier_release_kept_is_still_offered_used_and_removed(docker):
    """Hosts hold versions under the old name, and its label says whose each one is.

    A chat starts on the image discovery returns, and a cleanup removes the
    ones discovery returns, so neither needs the name to be this release's.
    """
    clock = Clock()
    kept = _kept_earlier(docker, f"{LEGACY}:claude-code_2.1.300_{short_id(BASE)}", "2.1.300")
    # The neighbour's, under the very same name, is never this deployment's.
    theirs = _kept_earlier(
        docker, f"{LEGACY}:claude-code_2.1.299_{short_id(BASE)}", "2.1.299", NEIGHBOUR
    )
    downloads = _downloads(clock=clock)
    downloads.apply([_target()])

    # Offered, and ready for its target without being downloaded again…
    assert runtime_images.DownloadedVersions("docker", NAMESPACE).versions() == {
        "claude-code": ["2.1.300"]
    }
    assert _run_once(downloads) is None
    assert _reported(downloads)["state"] == "ready"
    assert docker.ran("create") == []
    # …copied into a chat from that very image…
    manager = FakeManager()
    runtime_install.ensure_downloaded_runtime(manager, None, "claude-code", "2.1.300")
    assert manager.runs[0][-1] == kept
    # …and removed once nothing needs it.
    downloads.apply([])
    _cleaned_up(downloads, clock)
    assert kept not in docker.images
    assert theirs in docker.images


def test_a_new_download_is_kept_under_the_new_name_and_the_old_build_goes(docker):
    """A new sandbox image downloads its targets again; nothing new goes under the old name."""
    probe = Probe()
    old = _kept_earlier(docker, f"{LEGACY}:claude-code_2.1.300_{short_id(OLD_BASE)}", "2.1.300")
    downloads = _downloads(probe)
    downloads.apply([_target()])

    assert _run_once(downloads) is not None

    (kept,) = discover("docker", NAMESPACE)
    assert kept.ref == f"{repository(NAMESPACE)}:claude-code_2.1.300_{short_id(BASE)}"
    assert not kept.ref.startswith(f"{LEGACY}:")
    # Its candidate too: the old one is a name neighbours share.
    (checked,) = probe.calls
    assert checked["image"].startswith(f"{repository(NAMESPACE)}-candidate:")
    assert not checked["image"].startswith(f"{LEGACY}-candidate:")
    assert old not in docker.images


def test_a_restarted_runner_removes_its_old_name_candidates_and_no_neighbours(docker):
    """Earlier releases named candidates for the namespace folded, a name neighbours share.

    Only this deployment's go: a neighbour's may still be under its check.
    """
    ours = _kept_earlier(
        docker, f"{LEGACY}-candidate:claude-code_2.1.300_{short_id(BASE)}", "2.1.300"
    )
    theirs = _kept_earlier(
        docker, f"{LEGACY}-candidate:claude-code_2.1.301_{short_id(BASE)}", "2.1.301", NEIGHBOUR
    )

    _downloads()._remove_leftovers()

    assert ours not in docker.images
    assert theirs in docker.images
