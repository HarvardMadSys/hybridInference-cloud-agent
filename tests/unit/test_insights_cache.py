"""Derived insights reuse current content without hiding deleted requests."""

from unittest.mock import Mock

from cloud_agent import insights_cache


def test_cache_reuses_content_but_invalidates_nonlatest_deletion(monkeypatch):
    insights_cache._cache.clear()
    derive = Mock(wraps=insights_cache.build_insights)
    monkeypatch.setattr(insights_cache, "build_insights", derive)
    messages = ["Review backend", "Fix security"]
    first = insights_cache.current_insights("alice", messages)
    assert insights_cache.current_insights("alice", messages) == first
    assert derive.call_count == 1
    insights_cache.current_insights("alice", messages[:1])
    insights_cache.current_insights("bob", messages[:1])
    assert derive.call_count == 3


def test_cache_is_bounded_without_storing_raw_requests():
    insights_cache._cache.clear()
    for index in range(140):
        insights_cache.current_insights("alice", [f"Unique private request {index}"])
    assert len(insights_cache._cache) == 128
    assert all(isinstance(key[1], bytes) and len(key[1]) == 32 for key in insights_cache._cache)
