"""Churn, expiry and LRU behavior of repository answer caches."""

import logging

from cloud_agent.github_cache import RepositoryCache


def test_lru_does_not_extend_ttl_and_expiry_sweeps_unrelated_keys(monkeypatch, caplog):
    now = [100.0]
    monkeypatch.setattr("cloud_agent.github_cache.time.monotonic", lambda: now[0])
    caplog.set_level(logging.DEBUG, logger="cloud_agent.github_cache")
    cache = RepositoryCache[str, int]("test", max_size=2)
    cache.remember("old", 1, ttl=10)
    now[0] += 1
    cache.remember("second", 2, ttl=10)
    assert cache.get("old", ttl=10) == 1
    cache.remember("new", 3, ttl=10)
    assert cache.get("second", ttl=10) is None
    assert len(cache) == 2
    now[0] = 111
    assert cache.get("unrelated", ttl=10) is None
    assert len(cache) == 0
    assert caplog.records[-1].expired_total == 2
    assert caplog.records[-1].evicted_total == 1
    assert caplog.records[-1].entries == 0


def test_replacement_resets_insertion_order_and_setting_changes_expire_immediately(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("cloud_agent.github_cache.time.monotonic", lambda: now[0])
    cache = RepositoryCache[str, int]("test", max_size=2)
    cache.remember("first", 1, ttl=10)
    now[0] += 1
    cache.remember("second", 2, ttl=10)
    now[0] += 1
    cache.remember("first", 3, ttl=10)
    now[0] = 111
    assert cache.get("first", ttl=10) == 3
    assert list(cache) == ["first"]
    assert cache.get("first", ttl=1) is None
    cache.remember("disabled", 4, ttl=0)
    assert len(cache) == 0
