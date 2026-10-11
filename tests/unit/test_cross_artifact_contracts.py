"""Every fact this repository states twice, checked against its producer.

Three outages in one afternoon had the same shape: one fact written in two
artifacts that are built separately, and a test suite that read the copy instead
of the original.

* `FI_OBSERVER_VERSION` (C header) against `GUEST_OBSERVER_VERSION` (Python) --
  the guest stamped 2, the host demanded 1, and every observation failed.
* `org.freeinference.helper.guest-observer.version` (image label) against
  `SHARED_SANDBOX_HELPER_PINS` -- the image published 2, the pin said 1, and no
  runner could attest at all.
* `SHARED_SANDBOX_RUNTIME_PINS` against the image's build args -- the same
  shape, not yet triggered, and the pair most often edited.

Each time, the fixtures built both sides of the comparison from the Python
constant (`"version": 1`, `dict(SHARED_SANDBOX_HELPER_PINS)`), so the two agreed
by construction whatever the other artifact said. Every check here reads the
other artifact: the Dockerfile, or the C header. That is the whole point, and it
is why these live together rather than beside the code that happens to use them.

What this file cannot do is notice a *new* duplicated fact. When you write a
constant that something outside Python also states, add it here.
"""

from __future__ import annotations

import pathlib
import re

import pytest

from cloud_agent_host import member_resources, shared_protocol
from cloud_agent_host.guest_observer import GUEST_OBSERVER_VERSION
from cloud_agent_host.sandbox import SANDBOX_PATH

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SANDBOX_DOCKERFILE = REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox"
OBSERVER_HEADER = REPO_ROOT / "deploy" / "sandbox" / "guest-observer.h"


def dockerfile() -> str:
    return SANDBOX_DOCKERFILE.read_text(encoding="utf-8")


def header() -> str:
    return OBSERVER_HEADER.read_text(encoding="utf-8")


def header_define(name: str) -> str:
    """One ``#define`` value from the guest header, without its quotes."""
    match = re.search(rf"^#define\s+{re.escape(name)}\s+(.+)$", header(), re.MULTILINE)
    assert match, f"guest-observer.h no longer defines {name}"
    return match.group(1).strip().strip('"')


def header_enum(enum_name: str) -> dict[str, int]:
    """The members of one C enum, with their shift expressions evaluated."""
    body = re.search(rf"enum\s+{re.escape(enum_name)}\s*\{{(.*?)\}};", header(), re.DOTALL)
    assert body, f"guest-observer.h no longer declares enum {enum_name}"
    found: dict[str, int] = {}
    for name, shift in re.findall(r"(FI_\w+)\s*=\s*1U?\s*<<\s*(\d+)", body.group(1)):
        found[name] = 1 << int(shift)
    assert found, f"enum {enum_name} yielded no members"
    return found


class TestTheObserverProtocolVersion:
    """The number the guest stamps is the number the host accepts."""

    def test_the_header_and_the_host_agree(self) -> None:
        assert int(header_define("FI_OBSERVER_VERSION")) == GUEST_OBSERVER_VERSION


class TestTheImagePins:
    """Attestation compares pins to image labels exactly, then returns nothing."""

    LABEL = re.compile(r"org\.freeinference\.helper\.([A-Za-z0-9._-]+)\.version=\"([^\"]*)\"")
    ARG = re.compile(r"^ARG\s+([A-Z0-9_]+)_VERSION=(.+)$", re.MULTILINE)

    def helper_labels(self) -> dict[str, str]:
        found = dict(self.LABEL.findall(dockerfile()))
        assert found, "the sandbox image no longer declares helper version labels"
        return found

    def runtime_build_args(self) -> dict[str, str]:
        found = {
            name.lower().replace("_", "-"): value.strip()
            for name, value in self.ARG.findall(dockerfile())
        }
        assert found, "the sandbox image no longer declares runtime build args"
        return found

    @pytest.mark.parametrize("helper_id", sorted(shared_protocol.SHARED_SANDBOX_HELPER_PINS))
    def test_every_pinned_helper_matches_the_image(self, helper_id: str) -> None:
        labels = self.helper_labels()

        assert helper_id in labels, (
            f"{helper_id} is attested but the sandbox image publishes no label for it"
        )
        assert labels[helper_id] == shared_protocol.SHARED_SANDBOX_HELPER_PINS[helper_id]

    @pytest.mark.parametrize("runtime_id", sorted(shared_protocol.SHARED_SANDBOX_RUNTIME_PINS))
    def test_every_pinned_runtime_matches_the_image(self, runtime_id: str) -> None:
        """The pair most often edited, and the next outage of this shape.

        A runtime bump that touches the Dockerfile and not the pin -- or the
        other way round -- makes `_shared_live_host_capabilities` return an
        empty list, which reads as "this host cannot attest shared-sandbox-v1"
        rather than as a version mismatch.
        """
        build_args = self.runtime_build_args()

        assert runtime_id in build_args, (
            f"{runtime_id} is pinned but the sandbox image builds no {runtime_id} version"
        )
        assert build_args[runtime_id] == shared_protocol.SHARED_SANDBOX_RUNTIME_PINS[runtime_id]


class TestTheGuestPaths:
    """A path the host invokes and the guest ships must be one path."""

    def test_the_window_driver_is_where_the_guest_header_says(self) -> None:
        assert header_define("FI_WINDOW_DRIVER") == member_resources.MEMBER_WINDOW_DRIVER

    def test_the_image_installs_the_window_driver_at_that_path(self) -> None:
        assert member_resources.MEMBER_WINDOW_DRIVER in dockerfile()


class TestTheSandboxPath:
    """The image proves its tools under the PATH the runner launches them with."""

    def test_the_toolchain_check_runs_under_the_runners_path(self) -> None:
        assert f"PATH={SANDBOX_PATH};" in dockerfile()


class TestTheMirroredCounterBits:
    """Bit values the host re-declares instead of importing."""

    def test_window_counter_bits_match_the_guest_enum(self) -> None:
        guest = header_enum("fi_window_counter")

        assert {name: guest[name] for name in guest} == {
            "FI_WINDOW_CPU": member_resources.WINDOW_CPU,
            "FI_WINDOW_STORAGE_IO": member_resources.WINDOW_STORAGE_IO,
            "FI_WINDOW_PSS": member_resources.WINDOW_PSS,
            "FI_WINDOW_PSS_COMPLETE": member_resources.WINDOW_PSS_COMPLETE,
            "FI_WINDOW_ADDRESS_SPACES_DEDUPED": member_resources.WINDOW_ADDRESS_SPACES_DEDUPED,
            "FI_WINDOW_TRUNCATED": member_resources.WINDOW_TRUNCATED,
            "FI_WINDOW_READING_COMPLETE": member_resources.WINDOW_READING_COMPLETE,
        }

    def test_exit_counter_bits_match_the_guest_enum(self) -> None:
        guest = header_enum("fi_process_counter")

        assert guest["FI_COUNTER_CPU"] == member_resources.EXIT_COUNTER_CPU
        assert guest["FI_COUNTER_IO"] == member_resources.EXIT_COUNTER_IO


class TestTheStatusObject:
    """Field names the guest writes and the host requires."""

    def test_the_guest_publishes_every_field_parse_status_requires(self) -> None:
        """A renamed or dropped field reads as "the observer is not ready".

        `parse_status` refuses the whole object when one integer is missing, and
        the caller turns that into an unavailable cursor -- which surfaces as an
        observer that cannot be reached rather than as a field that moved.
        """
        writer = (REPO_ROOT / "deploy" / "sandbox" / "guest-observer.c").read_text(encoding="utf-8")
        published = set(re.findall(r'\\"([a-z_]+)\\":', writer))
        required = {"pid", "poll_seq", "event_seq", "lost", "journal_dropped"} | {
            "journal_bytes",
            "journal_dev",
            "journal_ino",
            "version",
            "ready",
        }

        assert required <= published, f"the guest no longer publishes {required - published}"
