"""The resident pool's ledger-following behaviour (design §7).

The control plane is authority; this pool is a cache of resident workspaces —
and, once a live turn has run, their processes — that must never disagree
with it for long:
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
        self.credential: str | None = "grant-token"
        self.released: list[tuple[str, int]] = []

    def establish(self, **_kwargs) -> dict | None:
        if self.refuse:
            return None
        self.generation += 1
        return {"generation": self.generation, "credential": self.credential, "model": "m"}

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

    assert _offer(pool, "t1") is None
    assert pool.lookup("t1") is None
    assert pool.beats() == []


def test_a_refreshed_offer_updates_the_session_id_without_a_second_establish():
    """Resume writes the continuation under a new id; the entry must follow."""
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1", "sess-turn-1")

    assert _offer(pool, "t1", "sess-turn-2") == 1

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


def test_a_generation_scoped_drop_never_ends_a_successor():
    """The receipt, redeemed all the way down (review, round 3): a cleanup
    holding generation N's receipt is a no-op once generation N+1 owns the
    thread — only an unscoped drop may end whatever currently holds it."""
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1")  # generation 1
    pool.drop("t1", release=True, generation=1)
    assert pool.lookup("t1") is None
    _offer(pool, "t1")  # generation 2 — the successor

    pool.drop("t1", release=True, generation=1)  # a stale receipt

    entry = pool.lookup("t1")
    assert entry is not None and entry.generation == 2


class _Session:
    """A live process stand-in that records whether the pool closed it."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_offer_records_the_residency_grant():
    control = _ControlPlane()
    pool = _pool(control)

    _offer(pool, "t1")
    entry = pool.lookup("t1")
    assert entry is not None
    assert entry.credential == "grant-token"
    assert entry.grant_model == "m"
    assert entry.live_session is None  # no live turn has run yet


def test_a_grantless_establish_keeps_a_workspace_only_entry():
    control = _ControlPlane()
    control.credential = None
    pool = _pool(control)

    _offer(pool, "t1")
    entry = pool.lookup("t1")
    assert entry is not None and entry.credential is None


def test_attach_session_is_generation_scoped_and_closes_on_mismatch():
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1")  # generation 1

    stale = _Session()
    assert pool.attach_session("t1", 2, stale) is False
    assert stale.closed  # refused sessions must not leak their process

    live = _Session()
    assert pool.attach_session("t1", 1, live) is True
    entry = pool.lookup("t1")
    assert entry is not None and entry.live_session is live and not live.closed


def test_reattaching_the_same_session_does_not_close_it():
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1")

    live = _Session()
    assert pool.attach_session("t1", 1, live)
    # The next settled turn re-attaches the session it borrowed; closing the
    # "stale" one would kill the process that was just handed back.
    assert pool.attach_session("t1", 1, live)
    assert not live.closed


def test_attach_replaces_and_closes_a_previous_session():
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1")

    first, second = _Session(), _Session()
    pool.attach_session("t1", 1, first)
    pool.attach_session("t1", 1, second)
    assert first.closed and not second.closed


def test_detach_closes_the_process_but_keeps_the_workspace():
    control = _ControlPlane()
    pool = _pool(control)
    _offer(pool, "t1")

    live = _Session()
    pool.attach_session("t1", 1, live)
    pool.detach_session("t1")
    assert live.closed
    entry = pool.lookup("t1")
    assert entry is not None and entry.live_session is None


def test_every_retirement_path_closes_an_attached_session():
    control = _ControlPlane()
    pool = _pool(control, budget=1)

    dropped = _Session()
    _offer(pool, "t1")
    pool.attach_session("t1", 1, dropped)
    pool.drop("t1", release=True)
    assert dropped.closed

    evicted = _Session()
    _offer(pool, "t2")
    pool.attach_session("t2", 2, evicted)
    _offer(pool, "t3")  # budget 1: t2 is evicted
    assert evicted.closed

    lost = _Session()
    pool.attach_session("t3", 3, lost)
    pool.handle_lost([{"thread_id": "t3", "generation": 3}])
    assert lost.closed

    drained = _Session()
    _offer(pool, "t4")
    pool.attach_session("t4", 4, drained)
    pool.close_all()
    assert drained.closed
