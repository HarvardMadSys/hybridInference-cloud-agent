"""SIGTERM finishes the job in hand instead of killing it.

Docker sends SIGTERM to replace a container, and Python's default handler
terminates the process. The runner only ever caught ``KeyboardInterrupt``,
which is SIGINT — so every ``docker compose up -d`` that recreated a runner
abandoned whatever job it was running.

That was survivable while deploys were occasional and manual. Since CD it
happens on every merge to dev, and it did: the first real job on this
deployment lost its first attempt to a deploy that landed a minute after it
started, and only the lease reaper noticed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap


def test_sigterm_sets_the_flag_and_does_not_kill_the_process() -> None:
    """The whole bug in one assertion.

    Run it in a child, because installing a SIGTERM handler in the test process
    and sending it a signal is a good way to lose a test runner. The child
    prints what it saw, and its exit code is the thing that used to be wrong:
    a process killed by SIGTERM exits ``-15``.
    """
    program = textwrap.dedent(
        """
        import os, signal, sys, time
        sys.path.insert(0, os.environ["HOST_PKG"])
        from cloud_agent_host import runner

        runner._drain_on_sigterm()
        os.kill(os.getpid(), signal.SIGTERM)

        # The handler runs on the main thread at the next bytecode boundary.
        for _ in range(100):
            if runner._STOPPING.is_set():
                break
            time.sleep(0.01)

        print("STOPPING" if runner._STOPPING.is_set() else "NOT_SET")
        sys.exit(0)
        """
    )
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env = {**os.environ, "HOST_PKG": os.path.join(here, "host")}

    done = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=30, env=env
    )

    assert done.returncode == 0, (
        f"the child died instead of handling SIGTERM "
        f"(rc={done.returncode}, -{signal.SIGTERM} means killed): {done.stderr[-400:]}"
    )
    assert "STOPPING" in done.stdout
    assert "finishing the current job" in done.stderr


def test_sigint_is_left_alone() -> None:
    """Ctrl-C still means now.

    Turning an interactive interrupt into a several-minute wait for a job to
    end would be its own surprise, and the two signals arrive from different
    people: SIGTERM from docker, which is already waiting out its grace
    period, and SIGINT from somebody at a keyboard.
    """
    from cloud_agent_host import runner

    before = signal.getsignal(signal.SIGINT)
    try:
        runner._drain_on_sigterm()
        assert signal.getsignal(signal.SIGINT) is before
    finally:
        signal.signal(signal.SIGINT, before)
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
