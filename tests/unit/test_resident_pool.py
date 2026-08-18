"""The resident pool's ledger-following behaviour (design §7).

The control plane is authority; this pool is a cache of resident *workspaces*
(no process lives between turns) that must never disagree with it for long:
refused establishes keep nothing, a lost beat retires an entry only when the
generation matches — a late answer to an old beat must not end the successor —
the budget evicts LRU with a release, and every entry an offer refreshes
carries the newest session id, because resume writes continuations under new
ids.
"""

from __future__ import annotations

from cloud_agent_host.residency import ResidentPool


class _ControlPlane:
    """Scripted establish answers; recorded releases."""

    def __init__(self) -> None:
        self.generation = 0
        self.refuse = False
        self.released: list[tuple[str, int]] = []

    def establish(self, **_kwargs) -> int | None:
        if self.refuse:
            return None
        self.generation += 1
        return self.generation

    def release(self, thread_id: str, generation: int) -> None:
        self.released.append((thread_id, generation))


def _pool(control: _ControlPlane, *, ttl_s: float = 1800.0, budget: int = 2) -> ResidentPool:
    return ResidentPool(
        establish=control.establish, release=control.release, ttl_s=ttl_s, budget=budget
    )


def _offer(pool: ResidentPool, thread_id: str, session_id: str = "sess-1") -> bool:
    return pool.offer(
        thread_id=thread_id,
        session_id=session_id,
        workdir=f"/w/{thread_id}",
        state_dir=f"/s/{thread_id}",
        runtime_id="claude-code",
        runtime_version="1.2.3",
        model="m",
    )


def test_a_refused_establish_keeps_nothing():
    control = _ControlPlane()
    control.refuse = True
    pool = _pool(control)

    assert _offer(pool, "t1") is False
    assert pool.lookup("t1") is None
    assert pool.beats() == []


def test_a_refreshed_offer_updates_the_session_id_without_a_second_establish():
    """Resume writes the continuation under a new id; the entry must follow."""
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1", "sess-turn-1")

    assert _offer(pool, "t1", "sess-turn-2") is True

    entry = pool.lookup("t1")
    assert entry is not None and entry.session_id == "sess-turn-2"
    assert control.generation == 1  # one establish, refreshed in place


def test_the_budget_evicts_lru_with_a_release():
    control = _ControlPlane()
    pool = _pool(control, budget=2)
    _offer(pool, "t1")
    _offer(pool, "t2")
    pool.touch("t1")  # t2 becomes the least recently used

    _offer(pool, "t3")

    assert pool.lookup("t2") is None
    assert ("t2", 2) in control.released
    assert pool.lookup("t1") is not None and pool.lookup("t3") is not None


def test_a_lost_beat_retires_only_its_own_generation():
    """Review finding 5's race, made deterministic.

    A heartbeat carrying gen 1 can be answered *after* this host already
    re-established gen 2. The stale answer must not end the successor; the
    verdict about gen 2 must.
    """
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1")  # generation 1
    pool.drop("t1", release=True)
    _offer(pool, "t1")  # generation 2 — the successor

    pool.handle_lost([{"thread_id": "t1", "generation": 1}])
    assert pool.lookup("t1") is not None  # the late answer changed nothing

    pool.handle_lost([{"thread_id": "t1", "generation": 2}])
    assert pool.lookup("t1") is None
    # A lost generation has nothing left to release.
    assert ("t1", 2) not in control.released


def test_a_generationless_loss_is_ignored():
    """An answer that cannot say which residency it means cannot end one."""
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1")

    pool.handle_lost([{"thread_id": "t1"}, {"generation": 1}, {}])

    assert pool.lookup("t1") is not None


def test_sweep_retires_entries_idle_past_the_ttl():
    control = _ControlPlane()
    pool = _pool(control, ttl_s=0.0)  # everything is instantly past its lease
    _offer(pool, "t1")

    pool.sweep()

    assert pool.lookup("t1") is None
    assert ("t1", 1) in control.released


def test_close_all_drains_everything_with_releases():
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1")
    _offer(pool, "t2")

    pool.close_all()

    assert sorted(control.released) == [("t1", 1), ("t2", 2)]
    assert pool.beats() == []
