"""What the control plane and a host say about agent CLIs downloaded on demand.

An admin updates an agent from the Settings page. The control plane turns that
into *targets* — exact versions every host should hold — and sends them on each
heartbeat answer. A host downloads each version it lacks, checks it with one
real turn (``cloud_agent_host.runtime_probe``), and reports its progress on the
next heartbeat. Once every online host holds an update, new chats start using
it.

A runtime has at most two targets: the version an update is moving new chats
to, and the downloaded version it replaced. Until the switch new chats still
use that one, and a host that joins while an update is still downloading, or
after it failed, must be able to run the version every other host runs. After
the switch it is what Roll back returns to, kept on every host so that rolling
back is instant.

Both images ship this module, so both sides validate the same shapes: a host
newer or older than its control plane drops what it cannot read rather than
acting on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

#: The npm registry both sides use when a deployment names none: the control
#: plane to learn the latest version, a host to download it. Each reads its own
#: ``AGENT_RUNTIME_REGISTRY_URL``.
DEFAULT_REGISTRY_URL = "https://registry.npmjs.org"

#: The version shape ``deploy/sandbox/install-runtimes.sh`` accepts. npm reads a
#: partial version as a range and a dist-tag as whatever it resolves to today,
#: and the version stamp on every attempt must name exactly one binary.
EXACT_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
VERSION_MAX_CHARS = 64

#: Targets per runtime: the version an update moves new chats to, and the one
#: it replaced (in use until the switch, Roll back's after).
MAX_TARGETS_PER_RUNTIME = 2
#: The most reports one heartbeat may carry; more is not a host of this release.
MAX_REPORTS = 32

#: The host is fetching, installing or checking the version.
DOWNLOADING = "downloading"
#: The version passed its check; this host can start chats on it.
READY = "ready"
#: The download or the check failed; ``error`` says why. A new request from
#: the admin (a new nonce) is what makes the host try again.
FAILED = "failed"
DOWNLOAD_STATES = frozenset({DOWNLOADING, READY, FAILED})
#: Long enough for "npm could not reach the registry" plus a tail of its output.
DOWNLOAD_ERROR_MAX_CHARS = 300
#: An opaque per-request token; a retry is a new one.
NONCE_MAX_CHARS = 64
_NONCE = re.compile(r"^[0-9A-Za-z-]{1,64}$")
#: ``npm install --before``: an ISO-8601 UTC instant.
_BEFORE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def is_exact_version(value: object) -> bool:
    """Whether ``value`` names one exact version, as the image build requires."""
    return (
        isinstance(value, str)
        and len(value) <= VERSION_MAX_CHARS
        and EXACT_VERSION.fullmatch(value) is not None
    )


def reports_version(output: str, version: str) -> bool:
    """Whether a CLI's ``--version`` output names exactly ``version``.

    The rule ``install-runtimes.sh`` applies, read from the last line the CLI
    printed: the version must appear as a whole token, so ``2.1`` is not in
    ``2.1.220``, nor ``0.145.0`` in ``10.145.0`` or ``0.145.0-alpha.1``.
    """
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines:
        return False
    token = re.compile(r"(?<![0-9.])" + re.escape(version) + r"(?![0-9A-Za-z.+-])")
    return token.search(f" {lines[-1]} ") is not None


@dataclass(frozen=True)
class DownloadTarget:
    """One version every host should hold for one runtime.

    ``before`` pins npm's dependency resolution to the instant the admin first
    asked for this version, so a package whose dependencies are ranges (pi's
    are) installs the same tree on a host that downloads it a week later. A
    retry keeps it and changes only the nonce.
    """

    runtime: str
    version: str
    before: str
    nonce: str

    def to_wire(self) -> dict[str, str]:
        """The target as a heartbeat answer carries it."""
        return {
            "runtime": self.runtime,
            "version": self.version,
            "before": self.before,
            "nonce": self.nonce,
        }

    @classmethod
    def from_wire(cls, value: object) -> DownloadTarget | None:
        """The target ``value`` describes, or None if any part is unusable."""
        if not isinstance(value, dict):
            return None
        runtime = value.get("runtime")
        version = value.get("version")
        before = value.get("before")
        nonce = value.get("nonce")
        if not (
            isinstance(runtime, str)
            and 0 < len(runtime) <= 64
            and is_exact_version(version)
            and isinstance(before, str)
            and _BEFORE.fullmatch(before)
            and isinstance(nonce, str)
            and _NONCE.fullmatch(nonce)
        ):
            return None
        return cls(runtime=runtime, version=str(version), before=before, nonce=nonce)


@dataclass(frozen=True)
class DownloadReport:
    """What one host says about one target."""

    runtime: str
    version: str
    nonce: str
    state: str
    #: 0 to 100, for the progress bar. Only ``downloading`` uses the middle.
    progress: int = 0
    error: str | None = None

    def to_wire(self) -> dict[str, Any]:
        """The report as a heartbeat carries it."""
        return {
            "runtime": self.runtime,
            "version": self.version,
            "nonce": self.nonce,
            "state": self.state,
            "progress": self.progress,
            "error": self.error,
        }

    @classmethod
    def from_wire(cls, value: object) -> DownloadReport | None:
        """The report ``value`` describes, or None if any part is unusable."""
        if not isinstance(value, dict):
            return None
        runtime = value.get("runtime")
        version = value.get("version")
        nonce = value.get("nonce")
        state = value.get("state")
        progress = value.get("progress", 0)
        error = value.get("error")
        if not (
            isinstance(runtime, str)
            and 0 < len(runtime) <= 64
            and is_exact_version(version)
            and isinstance(nonce, str)
            and _NONCE.fullmatch(nonce)
            # Type first: an object or a list is unhashable, and the set
            # lookup would raise instead of dropping the entry.
            and isinstance(state, str)
            and state in DOWNLOAD_STATES
            and isinstance(progress, int)
            and not isinstance(progress, bool)
            and 0 <= progress <= 100
            and (error is None or isinstance(error, str))
        ):
            return None
        return cls(
            runtime=runtime,
            version=str(version),
            nonce=nonce,
            state=state,
            progress=progress,
            error=None if error is None else error[:DOWNLOAD_ERROR_MAX_CHARS],
        )


def read_reports(value: object) -> list[DownloadReport]:
    """The usable reports in a heartbeat's ``runtime_downloads``, one per target.

    Anything unreadable is dropped, never the beat: a host from another release
    may report a runtime or a shape unknown here.
    """
    if not isinstance(value, list):
        return []
    reports: dict[tuple[str, str], DownloadReport] = {}
    for raw in value[:MAX_REPORTS]:
        report = DownloadReport.from_wire(raw)
        if report is not None:
            reports[(report.runtime, report.version)] = report
    return list(reports.values())


def find_report(reports: object, runtime: str, version: str) -> DownloadReport | None:
    """One host's report on ``runtime`` ``version``, from its stored reports."""
    for report in read_reports(reports):
        if (report.runtime, report.version) == (runtime, version):
            return report
    return None
