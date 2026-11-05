"""Take the member window censuses that are due, container by container.

A census is one Docker exec into a container's guest, answered by that guest's
single observer, which handles window requests one at a time. One container's
censuses therefore go out in order: sending them together would only queue
them inside the guest. Separate containers have separate guests, so they are
sampled at once: one after another, every concurrent turn on the host would
stretch the advertised census period of all the others.

Which censuses are due is each window's own answer
(:meth:`cloud_agent_host.member_window_census.MemberWindow.census_due`); this
module only takes them.
"""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor

from cloud_agent_host.member_resources import MemberWindowHandle, MemberWindowSampler

#: One window the census loop is about to sample, as it leaves the
#: supervisor's lock: the handle, its sampler and the census number. Named
#: because the hand-off is what lets the guest exec happen outside the lock,
#: and a reader has to be able to see that the handle and the sampler are
#: already known to be present.
DueCensus = tuple[MemberWindowHandle, MemberWindowSampler, int]

#: How many containers are sampled at once. The bound keeps a host with many
#: running turns from starting a burst of Docker clients every second.
MAX_PARALLEL_CENSUS_CONTAINERS = 8


def _take(censuses: list[DueCensus]) -> None:
    """One container's censuses, in order. A failed one is a missing cycle."""
    for window, sampler, sequence in censuses:
        with contextlib.suppress(Exception):
            sampler.sample(window, sequence)


def take_censuses(by_container: list[list[DueCensus]]) -> None:
    """Take every due census and return once all of them have answered.

    Waiting for the slowest container keeps the tick's order: the checkpoints
    built after it fold in every cycle these censuses journaled.
    """
    if len(by_container) <= 1:
        for censuses in by_container:
            _take(censuses)
        return
    with ThreadPoolExecutor(
        max_workers=min(len(by_container), MAX_PARALLEL_CENSUS_CONTAINERS),
        thread_name_prefix="shared-member-window-census",
    ) as pool:
        for censuses in by_container:
            pool.submit(_take, censuses)


__all__ = ["MAX_PARALLEL_CENSUS_CONTAINERS", "DueCensus", "take_censuses"]
