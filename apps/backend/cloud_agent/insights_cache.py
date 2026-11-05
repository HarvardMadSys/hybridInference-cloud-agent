"""Bounded derived-insight reuse after reading the current eligible request set."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from threading import Lock

from cloud_agent.work_insights import WorkInsights, build_insights

_cache: OrderedDict[tuple[str, bytes], WorkInsights] = OrderedDict()
_lock = Lock()


def current_insights(user_id: str, messages: list[str]) -> WorkInsights:
    """Reuse derivation only for exactly the current ordered message contents.

    Reads still check ownership and deleted-thread eligibility before this
    function. Deleting any contributing thread changes the key, even when
    the newest message belongs to another thread. No raw message is cached.
    """
    digest = hashlib.sha256(json.dumps(messages, ensure_ascii=False).encode()).digest()
    key = (user_id, digest)
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    result = build_insights(user_id, messages)
    with _lock:
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > 128:
            _cache.popitem(last=False)
    return result
