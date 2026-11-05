"""Taking the due member window censuses, container by container."""

from __future__ import annotations

import threading

from cloud_agent_host.member_resources import MemberWindowHandle
from cloud_agent_host.parallel_window_census import MAX_PARALLEL_CENSUS_CONTAINERS, take_censuses


class _Guest:
    """One container's observer: it records each census and how many overlap."""

    def __init__(self, host: _Host, name: str) -> None:
        self.host = host
        self.name = name
        self.sequences: list[int] = []

    def sample(self, handle: MemberWindowHandle, sample_seq: int) -> bool:
        with self.host.guard:
            self.host.in_flight += 1
            self.host.most = max(self.host.most, self.host.in_flight)
        try:
            self.sequences.append(sample_seq)
            if self.name in self.host.meet:
                self.host.rendezvous.wait()
            if sample_seq in self.host.failing:
                raise RuntimeError("docker exec failed")
            return True
        finally:
            with self.host.guard:
                self.host.in_flight -= 1


class _Host:
    def __init__(self, *, meet: set[str], parties: int, failing: set[int]) -> None:
        self.guard = threading.Lock()
        self.in_flight = 0
        self.most = 0
        self.meet = meet
        self.failing = failing
        self.rendezvous = threading.Barrier(parties, timeout=5)


def _window(name: str) -> MemberWindowHandle:
    return MemberWindowHandle(
        measurement_id=name,
        uid=11001,
        cursor=None,
        opened_host_mono=0.0,
        open_scan_ms=0,
    )


def test_a_burst_of_containers_is_sampled_within_the_bound():
    names = [f"container-{index}" for index in range(MAX_PARALLEL_CENSUS_CONTAINERS + 4)]
    # The first containers wait for one another: they only meet if that many
    # are sampled at once, and every later one would add to the overlap.
    host = _Host(
        meet=set(names[:MAX_PARALLEL_CENSUS_CONTAINERS]),
        parties=MAX_PARALLEL_CENSUS_CONTAINERS,
        failing=set(),
    )
    guests = [_Guest(host, name) for name in names]

    take_censuses([[(_window(guest.name), guest, 1)] for guest in guests])

    assert host.rendezvous.broken is False
    assert host.most == MAX_PARALLEL_CENSUS_CONTAINERS
    assert [guest.sequences for guest in guests] == [[1]] * len(guests)


def test_one_containers_censuses_go_in_order_past_a_failed_one():
    host = _Host(meet=set(), parties=1, failing={1})
    first, second = _Guest(host, "first"), _Guest(host, "second")

    take_censuses(
        [
            [(_window("a"), first, 1), (_window("b"), first, 2), (_window("c"), first, 3)],
            [(_window("d"), second, 7)],
        ]
    )

    # A failed census is a missing cycle, not the end of the container's tick.
    assert first.sequences == [1, 2, 3]
    assert second.sequences == [7]
