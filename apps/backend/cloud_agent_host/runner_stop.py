"""How the runner process stops: on SIGTERM after its job, or never on its own once refused.

Kept apart from :mod:`cloud_agent_host.runner`, which imports these under
their former private names.
"""

from __future__ import annotations

import contextlib
import signal
import sys
import threading

#: Set by SIGTERM. A container being replaced gets one, and until this existed
#: it killed the process outright — Python's default handler terminates, and
#: only ``KeyboardInterrupt`` (SIGINT) was caught. Every `docker compose up -d`
#: that recreated a runner therefore abandoned whatever job it was running.
#:
#: That is not only a deploy problem, but the deploy is where it bites on a
#: schedule: since CD, every merge to dev recreates these containers.
STOPPING = threading.Event()


def drain_on_sigterm() -> None:
    """Finish the job in hand on SIGTERM, then exit; SIGINT still stops now.

    The split is deliberate. `docker stop` sends SIGTERM; supported activation
    asks the Engine to wait indefinitely, so a job finishes before its runner
    is replaced. Direct, out-of-band Compose operations retain their configured
    grace period. A person pressing Ctrl-C means *now*, and turning that into a
    several-minute wait would be its own surprise.

    The lease machinery remains the backstop for a host failure or an explicit
    forced kill, not the deployment plan.
    """

    def handler(signum: int, frame: object) -> None:
        STOPPING.set()
        print(
            "agent runner: SIGTERM received; finishing the current job, then exiting",
            file=sys.stderr,
            flush=True,
        )

    # Only the main thread may install one; tests and embedders that call
    # `serve` from elsewhere get the old behaviour rather than an exception.
    with contextlib.suppress(ValueError):
        signal.signal(signal.SIGTERM, handler)


def stay_down() -> None:
    """Wait, doing nothing, until this refused host's container is stopped.

    Exiting would hand the container to its restart policy, and every start
    qualifies the machine again on a new self-test sandbox: a removed host
    being released would keep creating containers while its inventory is
    read. A redeploy stops this container and starts a new one, which
    enrolls with whatever credential it is then given.
    """
    with contextlib.suppress(KeyboardInterrupt):
        while not STOPPING.wait(timeout=60):
            pass
