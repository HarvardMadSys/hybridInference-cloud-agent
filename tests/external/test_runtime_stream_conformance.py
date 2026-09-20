"""Every version on the menu is one the event parser actually handles.

The gate for adding a version, and the check the sandbox Dockerfile's comment
has been asking for since it was written.

`runtimes.py` normalizes one event shape per runtime. Putting a version in a
``*_EXTRA_VERSIONS`` list is a claim that it parses — and until now nothing
tested that against a real CLI: the contract fixtures under
``tests/fixtures/agent_runtime_streams/`` are hand-written, down to
``"claude_code_version": "synthetic"``. They pin *our* mapping against a shape
we wrote down; they cannot notice that the shape changed.

The cost of being wrong is not hypothetical. An unpinned CLI floated to 2.1.220
once and every job died at its first model call, with an error naming the model
rather than the version. Offering a version an admin can select, without having
run it, puts that failure one dropdown click away.

So: run each offered version against a fake gateway, feed everything it emits
through that runtime's own normalizer, and require that the result is
*intelligible*, not merely non-empty.

Two bars, deliberately different:

* **Structural failures fail the version.** A line that is not JSON, or whose
  top-level ``type`` we do not handle, means the parser is broken for this
  build — the job would run and its transcript would be noise.
* **An unclassified ``system`` subtype does not.** That path is designed: the
  first occurrence is kept as a raw diagnostic and repeats are dropped,
  precisely so a CLI inventing another progress counter cannot flood the event
  log. Failing on it would block a version over telemetry nobody reads.

Run by hand — ``pytest -m external tests/external/test_runtime_stream_conformance.py``
— before adding a version to the image, and after any bump.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil

import pytest
from tests.support.claude_cli import ClaudeCli, FakeAnthropicGateway

from cloud_agent.thread_stats import summarize_thread
from cloud_agent_host.runtimes import ClaudeCodeRuntime

pytestmark = pytest.mark.external

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_DOCKERFILE = _REPO_ROOT / "deploy" / "sandbox" / "Dockerfile.agent-sandbox"

#: Raw reasons that mean the parser does not understand this build. Matched as
#: prefixes because two of them carry the offending value.
_STRUCTURAL = ("not json", "not an object", "unknown type")


def _menu() -> list[str]:
    """Every claude-code version the image offers, default first.

    Read from the Dockerfile rather than listed here: this suite must cover
    whatever the image was told to hold, including a version added an hour ago
    by someone who has not read this file.
    """
    dockerfile = _DOCKERFILE.read_text()
    default = re.search(r"^ARG CLAUDE_CODE_VERSION=(\S+)$", dockerfile, re.MULTILINE)
    assert default, "the sandbox Dockerfile no longer pins CLAUDE_CODE_VERSION"
    extra = re.search(r"^ARG CLAUDE_CODE_EXTRA_VERSIONS=(\S*)$", dockerfile, re.MULTILINE)
    versions = [default.group(1)]
    versions += [part.strip() for part in (extra.group(1) if extra else "").split(",")]
    return [version for version in versions if version]


@pytest.fixture
def gateway():
    fake = FakeAnthropicGateway()
    yield fake
    fake.close()


@pytest.fixture(autouse=True)
def _needs_npx():
    if shutil.which("npx") is None:
        pytest.skip("npx is required to run a real CLI")


@pytest.mark.parametrize("version", _menu())
def test_the_parser_understands_every_offered_claude_code_version(version, tmp_path, gateway):
    """One real turn per offered version, read through our own normalizer."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    config = tmp_path / "config"
    config.mkdir()

    cli = ClaudeCli(
        version=version,
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="conformance-key",
    )
    try:
        result = cli.turn("say ok")
    finally:
        cli.close()

    assert result is not None, (
        f"claude-code {version} never produced a result event. It is not a version "
        "this deployment can offer — the turn does not settle at all."
    )

    runtime = ClaudeCodeRuntime()
    events = [
        parsed
        for event in cli.transcript
        if (parsed := runtime.parse_event(json.dumps(event))) is not None
    ]

    unparsed = [
        event.payload.get("reason", "")
        for event in events
        if event.event_type == "raw"
        and str(event.payload.get("reason", "")).startswith(_STRUCTURAL)
    ]
    assert not unparsed, (
        f"claude-code {version} emits events runtimes.py cannot read: {unparsed}. "
        "Teach the normalizer this shape before offering the version."
    )

    kinds = [event.event_type for event in events]
    assert kinds and kinds[0] == "lifecycle", f"{version} did not open with a lifecycle event"

    init = next(
        (event for event in events if event.payload.get("phase") == "init"),
        None,
    )
    assert init is not None, f"claude-code {version} emitted no init event"
    # The CLI's own statement of what it is. It is the second, independent
    # source behind every version record — the image label says what was
    # installed, this says what ran — so a build whose binary disagrees with
    # its label is caught here rather than in a thread that forgets itself.
    assert init.payload.get("runtime_version") == version, (
        f"claude-code {version} reports itself as "
        f"{init.payload.get('runtime_version')!r} in its init event"
    )

    assert "lifecycle" in kinds[-1:], f"{version} did not close on a terminal lifecycle event"


def test_resident_claude_result_usage_is_per_turn_and_long_answers_survive(tmp_path, gateway):
    """Measure result scope on the pinned CLI, including a second resident turn."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    answer = "long answer detail\n" * 500 + "COMPLETE"
    gateway.reply_text = answer
    cli = ClaudeCli(
        version=_menu()[0],
        workdir=workdir,
        config_dir=config,
        gateway_port=gateway.port,
        credential="conformance-key",
    )
    runtime = ClaudeCodeRuntime()
    events = []
    try:
        for turn_no in [1, 2]:
            start = len(cli.transcript)
            result = cli.turn("say ok")
            assert result is not None and result["is_error"] is False
            # The fake gateway reports one token each way on each request.
            # A session-cumulative result would incorrectly report two here.
            assert result["usage"]["input_tokens"] == 1
            assert result["usage"]["output_tokens"] == 1
            normalized = [
                event
                for raw in cli.transcript[start:]
                if (event := runtime.parse_event(json.dumps(raw))) is not None
            ]
            assert any(
                event.event_type == "message" and event.payload["text"] == answer
                for event in normalized
            )
            for seq, event in enumerate(normalized):
                events.append(
                    {
                        "job_id": f"turn-{turn_no}",
                        "attempt_id": turn_no,
                        "seq": seq,
                        "event_type": event.event_type,
                        "payload": event.payload,
                    }
                )
    finally:
        cli.close()
    stats = summarize_thread(job_ids=["turn-1", "turn-2"], attempts=[], events=events)
    assert stats.input_tokens == 2
    assert stats.output_tokens == 2
