"""Notification-state cursors are bounded, owner-scoped, and structurally unambiguous."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import pytest

from cloud_agent.notification_state import (
    NotificationStateCursor,
    decode_notification_cursor,
    encode_cursor,
    notification_scope,
)


def _token(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


def test_bootstrap_and_change_cursors_round_trip_for_one_owner_and_page_size():
    bootstrap = NotificationStateCursor(
        scope=notification_scope("owner-a", 200),
        database_snapshot="100:104:101,103",
        bootstrap=True,
        bootstrap_at=datetime(2026, 10, 22, tzinfo=UTC),
        bootstrap_phase="active",
        after_id="job-200",
    )
    decoded = decode_notification_cursor(encode_cursor(bootstrap), user_id="owner-a", limit=200)
    assert decoded == bootstrap

    recent = NotificationStateCursor(
        scope=notification_scope("owner-a", 200),
        database_snapshot="100:104:101,103",
        bootstrap=True,
        bootstrap_at=datetime(2026, 10, 22, tzinfo=UTC),
        bootstrap_phase="recent",
        after_changed_at=datetime(2026, 10, 22, 1, tzinfo=UTC),
        after_id="job-400",
    )
    assert decode_notification_cursor(encode_cursor(recent), user_id="owner-a", limit=200) == recent

    changes = NotificationStateCursor(
        scope=notification_scope("owner-a", 200),
        database_snapshot="100:104:101,103",
        target_snapshot="104:108:",
        after_kind="thread",
        after_xid=105,
        after_id="thread-4",
    )
    assert (
        decode_notification_cursor(encode_cursor(changes), user_id="owner-a", limit=200) == changes
    )
    assert changes.version == 2


def test_v1_delta_continuation_remains_decodable_during_rollout():
    token = _token(
        {
            "version": 1,
            "scope": notification_scope("owner-a", 200),
            "database_snapshot": "100:104:101,103",
            "target_snapshot": "104:108:",
            "after_kind": "thread",
            "after_id": "thread-4",
        }
    )

    cursor = decode_notification_cursor(token, user_id="owner-a", limit=200)

    assert cursor is not None
    assert cursor.version == 1
    assert cursor.after_xid is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"database_snapshot": "bad"},
        {"database_snapshot": "10:9:"},
        {"database_snapshot": "10:20:11,11"},
        {"database_snapshot": "10:20:9"},
        {"bootstrap": True},
        {
            "bootstrap": True,
            "bootstrap_at": "2026-10-22T00:00:00",
        },
        {
            "bootstrap": True,
            "bootstrap_at": "2026-10-22T00:00:00Z",
            "target_snapshot": "20:21:",
        },
        {"after_id": "job-1"},
        {"bootstrap": True, "bootstrap_at": "2026-10-22T00:00:00Z", "after_id": ""},
        {
            "bootstrap": True,
            "bootstrap_at": "2026-10-22T00:00:00Z",
            "after_id": "job\u0000bad",
        },
        {
            "bootstrap": True,
            "bootstrap_at": "2026-10-22T00:00:00Z",
            "after_id": "job\ud800bad",
        },
        {
            "bootstrap": True,
            "bootstrap_at": "2026-10-22T00:00:00Z",
            "bootstrap_phase": "recent",
            "after_id": "job-1",
        },
        {
            "bootstrap": True,
            "bootstrap_at": "2026-10-22T00:00:00Z",
            "bootstrap_phase": "active",
            "after_changed_at": "2026-10-22T00:00:00Z",
            "after_id": "job-1",
        },
        {"bootstrap_phase": "active"},
        {"after_changed_at": "2026-10-22T00:00:00Z"},
        {"target_snapshot": "20:21:", "after_id": "job-1"},
        {"target_snapshot": "20:21:", "after_kind": "job"},
        {"version": 3},
        {
            "version": 2,
            "target_snapshot": "20:21:",
            "after_kind": "job",
            "after_id": "job-1",
        },
        {"after_xid": 10},
        {
            "target_snapshot": "20:21:",
            "after_kind": "job",
            "after_xid": 10,
            "after_id": "job-1",
        },
        {
            "target_snapshot": "20:21:",
            "after_kind": "job",
            "after_xid": 0,
            "after_id": "job-1",
        },
        {
            "target_snapshot": "20:21:",
            "after_kind": "job",
            "after_xid": True,
            "after_id": "job-1",
        },
    ],
)
def test_rejects_malformed_or_ambiguous_cursor_shapes(overrides):
    payload = {
        "version": 1,
        "scope": notification_scope("owner-a", 200),
        "database_snapshot": "10:20:",
        **overrides,
    }
    with pytest.raises(ValueError, match="Invalid notification-state cursor"):
        decode_notification_cursor(_token(payload), user_id="owner-a", limit=200)


def test_rejects_cursor_reuse_across_owners_page_sizes_and_oversized_input():
    cursor = NotificationStateCursor(
        scope=notification_scope("owner-a", 200),
        database_snapshot="10:20:",
    )
    token = encode_cursor(cursor)
    for owner, limit in (("owner-b", 200), ("owner-a", 199)):
        with pytest.raises(ValueError, match="Invalid notification-state cursor"):
            decode_notification_cursor(token, user_id=owner, limit=limit)
    with pytest.raises(ValueError, match="Invalid notification-state cursor"):
        decode_notification_cursor("x" * 32769, user_id="owner-a", limit=200)
