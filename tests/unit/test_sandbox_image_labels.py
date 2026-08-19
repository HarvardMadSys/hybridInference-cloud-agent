"""The sandbox image's version labels, locked to the registry and the pins.

The stamp pipeline (issue #54) is only as truthful as these labels: the
runner reads them, the heartbeat reports them, the claim copies them onto
attempts, and the checkpoint gate compares them before resuming a session.
A runtime added to the registry without a label degrades all of that to
"unknown" silently — this test is where that silence becomes a failure.
"""

from __future__ import annotations

import pathlib
import re

from cloud_agent_host.runtimes import registered_runtimes

_DOCKERFILE = (
    pathlib.Path(__file__).resolve().parents[2] / "deploy" / "sandbox" / "Dockerfile.agent-sandbox"
)


def test_every_registered_runtime_has_a_pinned_version_label():
    dockerfile = _DOCKERFILE.read_text()
    pinned_args = set(re.findall(r"^ARG ([A-Z_]+_VERSION)=\S+$", dockerfile, re.MULTILINE))
    for runtime_id in registered_runtimes():
        match = re.search(
            rf'org\.freeinference\.runtime\.{re.escape(runtime_id)}\.version="\$\{{([A-Z_]+)\}}"',
            dockerfile,
        )
        assert match, f"no version label for registered runtime {runtime_id!r}"
        # Derived from a pin, not typed by hand: a label carrying a literal
        # could drift from what npm actually installed.
        assert match.group(1) in pinned_args, f"{runtime_id!r} label names an unpinned ARG"
