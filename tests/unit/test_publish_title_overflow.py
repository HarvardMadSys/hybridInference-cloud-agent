"""A fallback draft title's "+N more" counts the work it leaves out.

The claim keeps the newest few closing summaries and counts them all. The
title collapses duplicates and skips summaries that do not read as a title, so
neither is more work; a summary the claim did not keep may be, and is counted.
"""

from __future__ import annotations

import pytest

from cloud_agent.publish_worker import _pr_title


@pytest.mark.parametrize(
    ("summaries", "summary_count", "expected"),
    [
        # Six turns that closed with the same summary name one change.
        (["Fix retries"] * 6, 6, "Fix retries"),
        # Six closing messages that read as no title name nothing.
        (["Done."] * 6, 6, "Update retry.py"),
        # Twelve turns, eight kept: two distinct here, and four never read.
        (["Fix retries", "Add retry docs"] * 4, 12, "Fix retries; Add retry docs; +4 more"),
        # Nothing readable among those kept: a count alone is no title.
        (["Done."] * 8, 14, "Update retry.py"),
    ],
)
def test_more_counts_only_the_summaries_left_out(summaries, summary_count, expected):
    job = {"session_summaries": summaries, "session_summary_count": summary_count}

    assert _pr_title(job, ["retry.py"]) == f"[agent] {expected}"
