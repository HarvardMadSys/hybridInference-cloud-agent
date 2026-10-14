"""Insights count the owner's own recent requests; they are not memory."""

from __future__ import annotations

from cloud_agent.work_insights import ACTIVITY_LIMIT, build_insights


def test_english_requests_produce_focus_areas_and_suggestions():
    insights = build_insights(
        "alice",
        [
            "Fix the login page crash on Safari",
            "Add a dark mode toggle to the React settings page",
            "Fix the flaky test in test_runner.py and run pytest to verify",
            "Review PR 612",
        ],
    )
    assert insights.focus_areas[0] == "Reliability and debugging"
    assert "Testing" in insights.focus_areas
    assert [suggestion.id for suggestion in insights.suggestions] == [
        "acceptance-checks",
        "verification",
        "reproduction",
    ]


def test_quoted_text_and_code_never_count():
    insights = build_insights("alice", ["> fix the crash\n```\nfix bug test\n```"])
    assert insights.focus_areas == [] and insights.suggestions == []


def test_the_avatar_keeps_its_companion_and_reflects_recent_work():
    plain = build_insights("alice", [])
    busy = build_insights("alice", ["Fix the backend API error"] * (ACTIVITY_LIMIT + 5))
    assert plain.avatar.data_url != busy.avatar.data_url
    assert plain.avatar.data_url == build_insights("alice", []).avatar.data_url
