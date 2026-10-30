"""A summary line that names no change is skipped when a pull request is titled."""

from __future__ import annotations

import pytest

from cloud_agent.pr_titles import is_generic_pr_summary


@pytest.mark.parametrize(
    "summary",
    [
        "Summary",
        "Done.",
        "I have successfully implemented the feature.",
        "The work is complete!",
        "Here's a brief summary of the changes:",
        "Completed。",
    ],
)
def test_a_completion_preamble_is_generic(summary):
    assert is_generic_pr_summary(summary)


@pytest.mark.parametrize(
    "summary",
    ["Fix retries and document backoff", "Add a button to restart failed runs", "changes.py"],
)
def test_a_summary_naming_the_change_is_kept(summary):
    assert not is_generic_pr_summary(summary)
