"""The lifecycle's diagnostic logs carry their facts in the message.

A host's default log format is text, which renders the message and drops the
``extra`` fields, and staging runs it. A log read there named its event and
nothing else, so a week of watching it on staging would have shown nothing.
The fields stay in ``extra`` for ``AGENT_LOG_FORMAT=json``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from tests.support.group_lifecycle import new_state

from cloud_agent_host.supervisor.group_lifecycle_state import (
    CombinationCounts,
    ExclusiveHolder,
    Hold,
    set_strict_checks,
)

if TYPE_CHECKING:
    import pytest

_LOGGER = "cloud_agent_host.supervisor.group_lifecycle"


def test_combinations_and_their_counts_name_what_was_seen(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ticks = iter([0.0, 0.0, 10.0])
    counts = CombinationCounts(clock=lambda: next(ticks), interval_s=5.0)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        counts.observe(("starting", "check"), site="admit")
        counts.observe(("starting", "check"), site="admit")

    assert [record.getMessage() for record in caplog.records] == [
        "group_lifecycle_combination starting+check at admit",
        "group_lifecycle_combination_counts over 5.0s: starting+check=2",
    ]
    assert caplog.records[0].__dict__["combination"] == "starting+check"


def test_a_violation_names_itself_and_the_combination(caplog: pytest.LogCaptureFixture) -> None:
    life = new_state(initial_archive_preservation_pending=True)
    life.exclusive = Hold(ExclusiveHolder.ENVIRONMENT)
    set_strict_checks(False)
    try:
        with life.condition, caplog.at_level(logging.WARNING, logger=_LOGGER):
            life.accepted_stopped_locked()
    finally:
        set_strict_checks(True)

    (record,) = caplog.records
    message = record.getMessage()
    assert message.startswith("group_lifecycle_violation: ")
    assert record.__dict__["violation"] in message
    assert f"(combination {record.__dict__['combination']})" in message
