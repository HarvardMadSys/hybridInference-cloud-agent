"""Agent CLI updates from the admin page: the shared wire shapes and the reduction.

The host side (download, check, copy into a chat) has its own tests; these pin
what both sides agree on and how the control plane turns every host's report
into the one state and one number the page shows.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import httpx
import pytest

from cloud_agent.runtime_upgrades import (
    REGISTRY_CACHE_S,
    STATE_DOWNLOADING,
    STATE_FAILED,
    STATE_UPDATED,
    RegistryClient,
    default_version,
    is_newer,
    stable_versions,
    upgrade_state,
)
from cloud_agent_shared.runtime_catalog import downloadable_runtimes
from cloud_agent_shared.runtime_downloads import (
    DOWNLOAD_ERROR_MAX_CHARS,
    DownloadReport,
    DownloadTarget,
    find_report,
    is_exact_version,
    read_reports,
    reports_version,
)

REPO = Path(__file__).resolve().parents[2]
NONCE = "0123456789abcdef0123456789abcdef"
TARGET = {
    "version": "2.1.300",
    "before": "2026-10-15T00:00:00Z",
    "nonce": NONCE,
    "previous_version": "2.1.235",
    "requested_at": "2026-10-15T00:00:00+00:00",
    "activated_at": None,
}


def _host(name: str, *, available=None, report=None, status="online", enrolled=True):
    return {
        "host": name,
        "status": status,
        "enrolled": enrolled,
        "runtime_versions": {"claude-code": "2.1.220"},
        "runtime_versions_available": {"claude-code": available or ["2.1.220"]},
        "runtime_downloads": [] if report is None else [report],
    }


def _report(
    state: str,
    progress: int = 0,
    *,
    nonce: str = NONCE,
    error: str | None = None,
    version: str = "2.1.300",
):
    return {
        "runtime": "claude-code",
        "version": version,
        "nonce": nonce,
        "state": state,
        "progress": progress,
        "error": error,
    }


# ── what is downloadable ──────────────────────────────────────────────────


#: Runtimes whose image default predates a move of their npm package: the
#: image still installs that default from the old name, and updates come from
#: the new one. pi 0.73.1 was the last ``@mariozechner`` release.
_IMAGE_PACKAGE_BEFORE_MOVE = {"pi": "@mariozechner/pi-coding-agent"}


def test_every_npm_runtime_but_kilo_can_be_updated():
    """Kilo runs a patched source build that npm does not publish."""
    catalog = downloadable_runtimes()
    assert sorted(catalog) == ["claude-code", "codex", "opencode", "pi"]
    # The same packages the image build installs, but for a recorded move.
    dockerfile = (REPO / "deploy/sandbox/Dockerfile.agent-sandbox").read_text()
    for runtime, definition in catalog.items():
        package = _IMAGE_PACKAGE_BEFORE_MOVE.get(runtime, definition.npm_package)
        assert f"install-runtimes.sh {runtime} {package} {definition.binary} " in dockerfile


def test_pi_updates_come_from_the_package_pi_moved_to():
    """Every ``@mariozechner/pi-coding-agent`` release is deprecated on npm."""
    assert downloadable_runtimes()["pi"].npm_package == "@earendil-works/pi-coding-agent"


# ── the version rules, shared with install-runtimes.sh ───────────────────


@pytest.mark.parametrize(
    "version",
    ["2.1", "latest", "^2.1.220", "~2.1.220", "2.1.x", "v2.1.220", "2.1.220 ", "2.1.220+b1", ""],
)
def test_only_an_exact_version_is_accepted(version):
    assert not is_exact_version(version)


@pytest.mark.parametrize("version", ["2.1.300", "0.162.1", "1.19.0-beta.1"])
def test_exact_versions_are_accepted(version):
    assert is_exact_version(version)


@pytest.mark.parametrize(
    ("reported", "version", "matches"),
    [
        ("2.1.220 (Claude Code)", "2.1.220", True),
        ("codex-cli 0.145.0", "0.145.0", True),
        ("0.73.1", "0.73.1", True),
        ("v1.18.9", "1.18.9", True),
        ("2.1.220 (Claude Code)", "2.1", False),
        ("2.1.220 (Claude Code)", "1.220", False),
        ("codex-cli 10.145.0", "0.145.0", False),
        ("codex-cli 0.145.0-alpha.1", "0.145.0", False),
        ("1.18.90", "1.18.9", False),
    ],
)
def test_a_binary_must_report_its_version_as_the_install_script_requires(
    tmp_path, reported, version, matches
):
    """The Python rule and the shell rule agree, case for case."""
    script = (REPO / "deploy/sandbox/install-runtimes.sh").read_text()
    start = script.index("reports_version() {")
    function = script[start : script.index("\n}\n", start) + 3]
    by_shell = subprocess.run(
        ["bash", "-c", function + 'reports_version "$1" "$2"', "-", reported, version],
        check=False,
    )
    assert (by_shell.returncode == 0) is matches
    assert reports_version(f"a banner line\n{reported}\n", version) is matches


def test_only_the_last_line_names_the_version():
    """``tail -1``, as the install script reads it."""
    assert not reports_version("2.1.300\nsomething else\n", "2.1.300")
    assert not reports_version("", "2.1.300")


# ── the wire shapes ───────────────────────────────────────────────────────


def test_a_target_survives_the_wire_and_an_unusable_one_does_not():
    target = DownloadTarget.from_wire(
        {"runtime": "claude-code", "version": "2.1.300", "before": TARGET["before"], "nonce": NONCE}
    )
    assert target is not None
    assert DownloadTarget.from_wire(target.to_wire()) == target
    for broken in (
        {**target.to_wire(), "version": "latest"},
        {**target.to_wire(), "before": "yesterday"},
        {**target.to_wire(), "nonce": "has spaces"},
        {**target.to_wire(), "runtime": ""},
        "not a dict",
    ):
        assert DownloadTarget.from_wire(broken) is None


def test_a_report_is_bounded_and_validated():
    report = DownloadReport.from_wire(_report("failed", error="x" * 1000))
    assert report is not None and len(report.error or "") == DOWNLOAD_ERROR_MAX_CHARS
    for broken in (
        _report("teleporting"),
        _report("downloading", 101),
        _report("downloading", -1),
        {**_report("downloading"), "progress": True},
        {**_report("downloading"), "version": "2.1"},
        {**_report("downloading"), "runtime": None},
        # Unhashable states are dropped like any other, not raised on.
        {**_report("downloading"), "state": {"nested": "object"}},
        {**_report("downloading"), "state": ["downloading"]},
    ):
        assert DownloadReport.from_wire(broken) is None


def test_reports_are_read_one_per_target_and_bounded():
    other = _report("ready", 100, version="2.1.235")
    reports = read_reports([_report("downloading", 5), other, "junk", _report("teleporting")])
    assert [(report.version, report.state) for report in reports] == [
        ("2.1.300", "downloading"),
        ("2.1.235", "ready"),
    ]
    assert read_reports({"claude-code": _report("ready")}) == []
    assert len(read_reports([_report("ready")] * 100)) == 1
    found = find_report([_report("downloading", 5), other], "claude-code", "2.1.235")
    assert found is not None and found.state == "ready"
    assert find_report([other], "codex", "2.1.235") is None


# ── reducing the hosts' reports ───────────────────────────────────────────


def test_progress_is_the_mean_of_the_online_hosts_and_stops_short_of_done():
    state = upgrade_state(
        "claude-code",
        TARGET,
        [
            _host("a", report=_report("downloading", 40)),
            _host("b", available=["2.1.220", "2.1.300"]),  # already offers it
            _host("c"),  # not reported yet
            # Neither of these is waited for.
            _host("d", status="draining", report=_report("downloading", 0)),
            _host("e", enrolled=False),
        ],
    )
    assert state is not None
    assert state.state == STATE_DOWNLOADING
    assert state.progress == round((40 + 100 + 0) / 3)
    assert state.previous_version == "2.1.235"

    every_host_ready = upgrade_state(
        "claude-code", TARGET, [_host("a", report=_report("ready", 100))]
    )
    assert every_host_ready is not None
    # Ready everywhere, but not switched yet: the bar only finishes at the switch.
    assert (every_host_ready.state, every_host_ready.progress) == (STATE_DOWNLOADING, 99)


def test_a_failure_on_any_host_is_the_state_and_names_the_host():
    state = upgrade_state(
        "claude-code",
        TARGET,
        [
            _host("a", report=_report("downloading", 70)),
            _host("b", report=_report("failed", error="npm could not reach the registry")),
        ],
    )
    assert state is not None
    assert state.state == STATE_FAILED
    assert state.error == "b: npm could not reach the registry"


def test_a_report_on_an_earlier_request_is_not_this_ones():
    """A retry is a new nonce: the failure it retries no longer counts."""
    state = upgrade_state(
        "claude-code",
        TARGET,
        [_host("a", report=_report("failed", nonce="f" * 32, error="old"))],
    )
    assert state is not None
    assert (state.state, state.progress, state.error) == (STATE_DOWNLOADING, 0, None)


def test_a_done_update_stays_done_while_a_host_checks_it_again():
    """After a new sandbox image, hosts download again; new chats already use it."""
    done = {**TARGET, "activated_at": "2026-10-15T00:05:00+00:00"}
    state = upgrade_state("claude-code", done, [_host("a", report=_report("downloading", 5))])
    assert state is not None
    assert (state.state, state.progress) == (STATE_UPDATED, 100)
    # …unless that check fails, which the admin has to hear about.
    failed = upgrade_state("claude-code", done, [_host("a", report=_report("failed", error="x"))])
    assert failed is not None and failed.state == STATE_FAILED


def test_a_report_on_the_version_in_use_is_not_the_updates():
    """While an update downloads, hosts also hold the version new chats use."""
    state = upgrade_state(
        "claude-code",
        TARGET,
        [_host("a", report=_report("failed", version="2.1.235", error="held copy"))],
    )
    assert state is not None
    assert (state.state, state.error) == (STATE_DOWNLOADING, None)


def test_a_report_on_the_version_roll_back_returns_to_is_not_the_updates():
    """After the switch, hosts also hold the version the update replaced."""
    done = {**TARGET, "activated_at": "2026-10-15T00:05:00+00:00"}
    held = _report("failed", version="2.1.235", nonce="a" * 32, error="held copy")
    state = upgrade_state(
        "claude-code", done, [_host("a", available=["2.1.220", "2.1.300"], report=held)]
    )
    assert state is not None
    assert (state.state, state.progress, state.error) == (STATE_UPDATED, 100, None)


def test_a_chosen_version_is_shown_only_when_a_host_cannot_download_it():
    """Roll back and a hand-picked version keep it on every host; no update to follow."""
    chosen = {
        **TARGET,
        "kind": "selection",
        "version": "2.1.235",
        "previous_version": None,
        "activated_at": "2026-10-15T00:05:00+00:00",
    }
    quiet = [_host("a", report=_report("downloading", 5, version="2.1.235"))]
    assert upgrade_state("claude-code", chosen, quiet) is None
    failed = upgrade_state(
        "claude-code",
        chosen,
        [_host("b", report=_report("failed", version="2.1.235", error="npm is down"))],
    )
    assert failed is not None
    assert (failed.state, failed.kind, failed.error) == (
        STATE_FAILED,
        "selection",
        "b: npm is down",
    )


def test_no_target_is_no_update():
    assert upgrade_state("claude-code", None, [_host("a")]) is None
    assert upgrade_state("claude-code", {"version": "latest"}, [_host("a")]) is None


def test_the_default_is_shown_only_when_every_online_host_agrees():
    assert default_version("claude-code", [_host("a"), _host("b")]) == "2.1.220"
    disagree = _host("b")
    disagree["runtime_versions"] = {"claude-code": "2.1.235"}
    assert default_version("claude-code", [_host("a"), disagree]) is None


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        ("2.1.300", "2.1.220", True),
        ("2.1.220", "2.1.220", False),
        ("2.1.100", "2.1.220", False),
        ("2.10.0", "2.9.9", True),
        ("2.1.300", "2.1.300-beta.1", True),
        ("2.1.300", None, True),
        (None, "2.1.220", False),
    ],
)
def test_newer_compares_versions_numerically(candidate, current, newer):
    assert is_newer(candidate, current) is newer


# ── asking npm ────────────────────────────────────────────────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_the_registry_is_asked_once_an_hour_per_package():
    asked: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        return httpx.Response(200, json={"name": "@anthropic-ai/claude-code", "version": "2.1.300"})

    clock = _Clock()
    registry = RegistryClient(
        base_url="https://registry.test", transport=httpx.MockTransport(answer), clock=clock
    )
    assert await registry.latest("@anthropic-ai/claude-code") == ("2.1.300", None)
    assert await registry.latest("@anthropic-ai/claude-code") == ("2.1.300", None)
    assert asked == ["/@anthropic-ai/claude-code/latest"]
    clock.now += REGISTRY_CACHE_S + 1
    await registry.latest("@anthropic-ai/claude-code")
    assert len(asked) == 2


async def test_an_unreachable_registry_is_a_message_and_is_asked_again_soon():
    asked = 0

    def answer(request: httpx.Request) -> httpx.Response:
        nonlocal asked
        asked += 1
        raise httpx.ConnectError("refused", request=request)

    clock = _Clock()
    registry = RegistryClient(
        base_url="https://registry.test", transport=httpx.MockTransport(answer), clock=clock
    )
    version, error = await registry.latest("opencode-ai")
    assert version is None
    assert error == "could not reach the npm registry: ConnectError"
    await registry.latest("opencode-ai")
    assert asked == 1
    clock.now += 61
    await registry.latest("opencode-ai")
    assert asked == 2


async def test_a_registry_answer_without_an_exact_version_is_not_offered():
    registry = RegistryClient(
        base_url="https://registry.test",
        transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"version": "next"})),
    )
    assert await registry.latest("@openai/codex") == (
        None,
        "npm returned no usable version for @openai/codex",
    )


@pytest.mark.parametrize(("status", "expected"), [(200, True), (404, False), (503, None)])
async def test_a_requested_version_is_checked_against_npm(status, expected):
    paths: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(status, json={})

    registry = RegistryClient(
        base_url="https://registry.test", transport=httpx.MockTransport(answer)
    )
    assert await registry.has_version("@mariozechner/pi-coding-agent", "0.74.0") is expected
    assert paths == ["/@mariozechner/pi-coding-agent/0.74.0"]


# ── every release, for the version menu ──────────────────────────────────


def _document(versions: dict[str, dict], latest: str) -> dict:
    return {"name": "pkg", "dist-tags": {"latest": latest}, "versions": versions}


def test_the_menu_lists_stable_releases_newest_first():
    found, note = stable_versions(
        _document(
            {
                "0.9.0": {},
                "0.10.0": {},
                "0.162.0-alpha.1": {},
                "0.0.0-snapshot-ui-202601010000": {},
                "0.54.0": {"deprecated": "bad MUSL binaries on Linux"},
                "0.162.1": {},
                "latest": {},
                "1.2": {},
            },
            "0.162.1",
        )
    )
    # Numeric order, not string order; no prerelease, no deprecated release.
    assert found == ["0.162.1", "0.10.0", "0.9.0"]
    assert note is None


def test_a_package_deprecated_as_a_whole_still_lists_every_release_and_says_why():
    """pi's package moved; every release is deprecated with the same message."""
    moved = {"deprecated": "please use @earendil-works/pi-coding-agent instead going forward"}
    found, note = stable_versions(
        _document({"0.72.1": moved, "0.73.1": moved, "0.73.1-rc.1": moved}, "0.73.1")
    )
    assert found == ["0.73.1", "0.72.1"]
    assert note == moved["deprecated"]


@pytest.mark.parametrize("document", [None, [], {}, {"versions": []}, {"versions": {"1.0.0": 1}}])
def test_an_unreadable_package_document_lists_nothing(document):
    assert stable_versions(document) == ([], None)


async def test_the_version_list_is_read_abbreviated_once_an_hour_and_once_at_a_time():
    asked: list[tuple[str, str]] = []

    def answer(request: httpx.Request) -> httpx.Response:
        asked.append((request.url.path, request.headers["accept"]))
        return httpx.Response(200, json=_document({"2.1.299": {}, "2.1.300": {}}, latest="2.1.300"))

    clock = _Clock()
    registry = RegistryClient(
        base_url="https://registry.test", transport=httpx.MockTransport(answer), clock=clock
    )
    first, second = await asyncio.gather(
        registry.versions("@anthropic-ai/claude-code"),
        registry.versions("@anthropic-ai/claude-code"),
    )
    assert first == second
    assert first.versions == ["2.1.300", "2.1.299"]
    assert (first.deprecated, first.error) == (None, None)
    # One read for both callers, of the document without every readme in it.
    assert len(asked) == 1
    path, accept = asked[0]
    assert path == "/@anthropic-ai/claude-code"
    assert accept.startswith("application/vnd.npm.install-v1+json")
    clock.now += REGISTRY_CACHE_S + 1
    await registry.versions("@anthropic-ai/claude-code")
    assert len(asked) == 2


async def test_an_unreadable_version_list_is_a_message_and_is_asked_again_soon():
    answers = iter(
        [
            httpx.Response(503),
            httpx.Response(200, content=b"not json"),
            httpx.Response(200, json=_document({"1.0.0-beta.1": {}}, latest="1.0.0-beta.1")),
        ]
    )
    clock = _Clock()
    registry = RegistryClient(
        base_url="https://registry.test",
        transport=httpx.MockTransport(lambda _request: next(answers)),
        clock=clock,
    )
    failed = await registry.versions("opencode-ai")
    assert (failed.versions, failed.error) == ([], "npm answered 503 for opencode-ai")
    assert (await registry.versions("opencode-ai")).error == failed.error  # cached
    clock.now += 61
    garbled = await registry.versions("opencode-ai")
    assert garbled.error == "could not reach the npm registry: JSONDecodeError"
    clock.now += 61
    empty = await registry.versions("opencode-ai")
    assert empty.error == "npm lists no stable release of opencode-ai"
