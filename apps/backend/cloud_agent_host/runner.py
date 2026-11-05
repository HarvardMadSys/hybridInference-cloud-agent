"""The trusted host runner that schedules turns into retained chat sandboxes.

It runs outside the chat container, claims one fenced attempt at a time, and
streams the member Agent CLI's output through the sandbox manager. Its shape
follows from the adjudicated security model rather than convenience:

- **Narrow credentials.** The enrolled host claims with its own identity. Only
  a member-scoped inference grant, an attempt-fenced MCP token, and a GitHub
  relay placeholder (``agh.``) that GitHub itself refuses enter the sandbox.
  The read-only clone token, the owner's GitHub token, and worker-control,
  account and service credentials stay outside.
- **The agent pushes, not the platform.** The runner never pushes. It clones
  with a read-only token, puts the worktree on a local ``agent/<thread>``
  branch and leaves a credential-free ``origin``. The agent commits, pushes
  and opens pull requests itself through the GitHub relay, as the
  conversation's owner.
- **Heartbeats are how cancellation arrives.** A background thread renews the
  lease and watches the reply for ``cancel_requested``; the agent is killed on
  the next beat. Losing the lease (409) is fatal by design — a runner whose
  attempt was superseded must stop rather than race its replacement.

Every step is written so that failing loudly beats continuing quietly: an
unparsable line becomes a raw event rather than disappearing, and a lost lease
ends the run instead of being retried.

This module is the command line. Without ``--turn`` it runs the host service
(:mod:`cloud_agent_host.service.host_service`); with it, one turn's child process
(:mod:`cloud_agent_host.turn.turn_child`). The trusted Git work on a chat's source
is :mod:`cloud_agent_host.turn.source_preparation`.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

from cloud_agent_host.logging_utils import configure_process_logging
from cloud_agent_host.retired_configuration import refuse_retired_host_configuration
from cloud_agent_host.service.attempt_lease import _UNRENEWABLE_LEASE_S
from cloud_agent_host.service.host_service import run_forever
from cloud_agent_host.service.local_settings import (
    DEFAULT_LEASE_TTL_S,
    _local_agent_timeout_s,
)
from cloud_agent_host.turn.turn_child import _serve_turn


def _lease_ttl_arg(raw: str) -> float:
    """Parse ``--lease-ttl``, refusing a lease that expires before its next renewal."""
    try:
        ttl = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {raw!r}") from exc
    if not ttl > _UNRENEWABLE_LEASE_S:
        raise argparse.ArgumentTypeError(
            f"{raw!r} is not longer than {_UNRENEWABLE_LEASE_S:g} seconds, one heartbeat "
            "interval plus one request timeout: such a lease expires before it is renewed"
        )
    return ttl


def _env_lease_ttl() -> float:
    """Read ``AGENT_LEASE_TTL``, keeping the default for a value no lease can use."""
    raw = os.environ.get("AGENT_LEASE_TTL")
    if not raw:
        return DEFAULT_LEASE_TTL_S
    try:
        return _lease_ttl_arg(raw)
    except argparse.ArgumentTypeError as exc:
        print(f"ignoring AGENT_LEASE_TTL: {exc}", file=sys.stderr)
        return DEFAULT_LEASE_TTL_S


def default_worker_id() -> str:
    """Identify this runner, distinctly from its replicas.

    ``lease_owner`` is how an operator answers "which runner has this job" and
    "which one is stuck". Replicas share an environment, so a plain
    ``AGENT_WORKER_ID`` makes every one of them report the same name — the
    question stops being answerable exactly when a second replica makes it
    worth asking. The hostname is unique per container, so it is appended
    rather than replaced: the configured value still groups a fleet.

    Not a correctness fix — fencing is on ``(attempt_id, lease_generation)``,
    never on this string.
    """
    base = (os.environ.get("AGENT_WORKER_ID") or "runner").strip() or "runner"
    host = socket.gethostname().strip()
    return f"{base}-{host}" if host and not base.endswith(host) else base


def default_runner_host() -> str | None:
    """Name the *machine* this runner sits on, for the host pool.

    Explicit configuration only. ``socket.gethostname()`` is what
    :func:`default_worker_id` uses and is exactly wrong here: inside a
    container it returns the container id, so every replica would enter the
    pool as its own "host" and every restart would add another one — an
    operator picking a machine would be choosing between rows of hex.

    The retained-only service requires this value (or enrollment supplies a
    configured name); an unreported host cannot claim shared work.
    """
    return (os.environ.get("AGENT_RUNNER_HOST") or "").strip() or None


def build_parser() -> argparse.ArgumentParser:
    """Build the runner CLI parser.

    Separate from :func:`main` so the deployment configuration can be checked
    against the flags that actually exist, rather than discovering a typo when
    a runner container crash-loops.
    """
    parser = argparse.ArgumentParser(description="Run queued agent jobs.")
    # The control plane is the only origin a host is configured with; owners
    # supply their own inference connections.
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AGENT_CONTROL_PLANE_URL", ""),
        help="Control plane: claim, heartbeat, events, lifecycle, terminals.",
    )
    parser.add_argument("--worker-id", default=default_worker_id())
    parser.add_argument(
        "--host",
        default=default_runner_host(),
        help=(
            "Machine this runner sits on (AGENT_RUNNER_HOST). Shared by every "
            "replica here; joins the pool an admin can switch between."
        ),
    )
    # The compose overlay sets AGENT_LEASE_TTL and AGENT_TIMEOUT_S, and
    # neither reached the runner: every self-hosted job used the built-in
    # defaults regardless of what the operator configured, silently.
    parser.add_argument(
        "--lease-ttl",
        type=_lease_ttl_arg,
        default=_env_lease_ttl(),
    )
    parser.add_argument(
        "--agent-timeout",
        type=float,
        default=_local_agent_timeout_s(),
    )
    # Not operator flags. The host service starts each turn's child with these
    # and the exact configuration it was given itself; the turn arrives on that
    # child's standard input and its answer goes back on the descriptor named
    # here. Started by hand, a turn process reads an empty standard input,
    # declines, and exits. A spare, started before its turn is claimed, reads
    # an empty one as being retired and exits without the probe.
    parser.add_argument("--turn", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--handover-fd", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--spare", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point used by the self-hosted runner service and local tools."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # In the source repo, importing sandbox configured root logging as a side
    # effect; the host shim removed that on purpose. This is the sanctioned
    # replacement (the extraction record's deferred-behaviour register, the
    # runner-logging row) — without it every INFO event a runner emits goes
    # nowhere.
    configure_process_logging()

    try:
        refuse_retired_host_configuration()
    except ValueError as exc:
        parser.error(str(exc))
    if not args.base_url:
        parser.error("AGENT_CONTROL_PLANE_URL is required")

    if args.turn:
        if args.handover_fd is None:
            parser.error("--turn requires --handover-fd; the host service supplies both")
        return _serve_turn(
            handover_fd=args.handover_fd,
            base_url=args.base_url,
            worker_id=args.worker_id,
            lease_ttl=args.lease_ttl,
            agent_timeout_s=args.agent_timeout,
            host=args.host,
            spare=args.spare,
        )
    if args.spare:
        parser.error("--spare is a turn process's flag; the host service supplies it with --turn")
    return run_forever(
        base_url=args.base_url,
        worker_id=args.worker_id,
        lease_ttl=args.lease_ttl,
        agent_timeout_s=args.agent_timeout,
        host=args.host,
    )


if __name__ == "__main__":
    raise SystemExit(main())
