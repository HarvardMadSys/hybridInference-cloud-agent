"""The resident pool's ledger-following behaviour (design §7).

The control plane is authority; this pool is a cache that must never disagree
with it for long: refused establishes keep nothing, lost beats tear down
without a release call, the budget evicts LRU with a release, and every path
out closes the session.
"""

from __future__ import annotations

from cloud_agent_host.residency import ResidentPool


class _Session:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


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


def _offer(pool: ResidentPool, thread_id: str, session: _Session) -> bool:
    return pool.offer(
        thread_id=thread_id,
        session=session,
        workdir=f"/w/{thread_id}",
        state_dir=f"/s/{thread_id}",
        runtime_id="claude-code",
        runtime_version="1.2.3",
        model="m",
    )


def test_a_refused_establish_keeps_nothing_and_closes_the_session():
    control = _ControlPlane()
    control.refuse = True
    pool = _pool(control)
    session = _Session()

    assert _offer(pool, "t1", session) is False
    assert session.closed is True
    assert pool.lookup("t1") is None


def test_the_budget_evicts_lru_with_a_release():
    control = _ControlPlane()
    pool = _pool(control, budget=2)
    first, second, third = _Session(), _Session(), _Session()
    _offer(pool, "t1", first)
    _offer(pool, "t2", second)
    pool.touch("t1")  # t2 becomes the least recently used

    _offer(pool, "t3", third)

    assert pool.lookup("t2") is None
    assert second.closed is True
    assert ("t2", 2) in control.released
    assert pool.lookup("t1") is not None and pool.lookup("t3") is not None


def test_a_lost_beat_tears_down_without_a_release_call():
    control = _ControlPlane()
    pool = _pool(control)
    session = _Session()
    _offer(pool, "t1", session)

    pool.handle_lost(["t1"])

    assert session.closed is True
    assert pool.lookup("t1") is None
    assert control.released == []  # a lost generation has nothing to release


def test_sweep_retires_entries_idle_past_the_ttl():
    control = _ControlPlane()
    pool = _pool(control, ttl_s=0.0)  # everything is instantly past its lease
    session = _Session()
    _offer(pool, "t1", session)

    pool.sweep()

    assert session.closed is True
    assert ("t1", 1) in control.released


def test_close_all_drains_everything_with_releases():
    control = _ControlPlane()
    pool = _pool(control)
    sessions = [_Session(), _Session()]
    _offer(pool, "t1", sessions[0])
    _offer(pool, "t2", sessions[1])

    pool.close_all()

    assert all(session.closed for session in sessions)
    assert sorted(control.released) == [("t1", 1), ("t2", 2)]
    assert pool.beats() == []
