"""``python -m deploy.agent``: set up, run and recover this machine's deployment agent.

Every operation the control plane can ask for has a local command here too,
executed through the same validation and journal, so a machine can be
recovered while the control plane or its database is unavailable. Local
commands run with this machine's own authority (whoever can run them can
already edit ``.env``); they need no control-plane credential.
"""

from __future__ import annotations

import argparse
import getpass
import json
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from deploy.agent import releases
from deploy.agent.client import ControlPlaneClient, ControlPlaneUnavailable, sync_once
from deploy.agent.deployment_contract import generate_keypair, seal, sealed_purpose
from deploy.agent.operations import Executor
from deploy.agent.store import AgentStore, StoreError, write_json
from deploy.agent.system import SubprocessRunner


def _store(args: argparse.Namespace) -> AgentStore:
    return AgentStore(Path(args.root).resolve())


def _executor(args: argparse.Namespace) -> Executor:
    from deploy.agent.database import register as register_database
    from deploy.agent.rotation import register as register_rotation

    executor = Executor(_store(args), SubprocessRunner())
    register_database(executor)
    register_rotation(executor)
    return executor


def cmd_init(args: argparse.Namespace) -> int:
    store = _store(args)
    if store.exists() and not args.force:
        print("this machine already has a deployment agent", file=sys.stderr)
        return 1
    pem, public = generate_keypair()
    store.save_private_key(pem)
    store.save_identity(
        {
            "name": args.name or socket.gethostname(),
            "role": args.role,
            "machine": socket.gethostname(),
            "control_plane_url": args.control_plane_url or "",
            "public_key": public,
            "agent_id": None,
        }
    )
    policy_path = store.root / "policy.json"
    if not policy_path.exists():
        write_json(policy_path, {"allowed": {}, "kata": [], "compose": {}}, 0o644)
    store.adopt_existing()
    print(json.dumps({"initialized": str(store.root), "public_key": public}))
    return 0


def cmd_register(args: argparse.Namespace) -> int:
    store = _store(args)
    identity = store.identity()
    url = args.control_plane_url or identity.get("control_plane_url")
    if not url:
        print("name the control plane with --control-plane-url", file=sys.stderr)
        return 1
    token = args.token or getpass.getpass("enrollment token: ")
    answer = ControlPlaneClient(url).register(token.strip(), identity)
    identity["agent_id"] = answer["agent_id"]
    identity["control_plane_url"] = url
    store.save_identity(identity)
    store.save_credential(answer["credential"])
    print(json.dumps({"agent_id": answer["agent_id"]}))
    return 0


def _client(store: AgentStore) -> ControlPlaneClient:
    identity = store.identity()
    credential = store.credential()
    if not identity.get("agent_id") or not credential:
        raise StoreError("this agent is not registered with a control plane")
    return ControlPlaneClient(str(identity["control_plane_url"]), credential)


def cmd_run(args: argparse.Namespace) -> int:
    executor = _executor(args)
    client = _client(executor.store)
    while True:
        try:
            sync_once(executor, client)
        except ControlPlaneUnavailable as exc:
            # The machine keeps running its last configuration; local
            # commands keep working. Try again next interval.
            print(f"deploy agent: {exc}; retrying", file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


def _show(outcome: Any) -> int:
    print(
        json.dumps({"state": outcome.state, "detail": outcome.detail, "evidence": outcome.evidence})
    )
    return 0 if outcome.state == "succeeded" else 1


def cmd_status(args: argparse.Namespace) -> int:
    executor = _executor(args)
    print(
        json.dumps(
            {
                "identity": {
                    k: v for k, v in executor.store.identity().items() if k != "public_key"
                },
                "observed": executor.observed(),
            },
            indent=2,
            default=str,
        )
    )
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    executor = _executor(args)
    changes = dict(pair.split("=", 1) for pair in args.assignments)
    sealed = {}
    for name in args.secret or []:
        value = getpass.getpass(f"{name}: ")
        sealed[name] = seal(
            executor.store.identity()["public_key"], value, sealed_purpose(executor.agent_id, name)
        )
    return _show(executor.execute("apply_config", {"changes": changes}, sealed))


def cmd_rollback(args: argparse.Namespace) -> int:
    return _show(_executor(args).execute("rollback_config", {"to_rev": args.rev}))


def cmd_restart(args: argparse.Namespace) -> int:
    return _show(_executor(args).execute("restart_services", {"services": args.services}))


def cmd_release(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.release_command == "import":
        entry = releases.import_release(store, Path(args.manifest), Path(args.recipe), args.sha256)
        print(json.dumps(entry, default=str))
        return 0
    if args.release_command == "list":
        print(json.dumps(store.release_lock(), indent=2, default=str))
        return 0
    operation = "release_" + args.phase.replace("-", "_")
    return _show(_executor(args).execute(operation, {"manifest_sha256": args.sha256}))


def cmd_kata(args: argparse.Namespace) -> int:
    return _show(
        _executor(args).execute("kata_install", {"version": args.version, "sha256": args.sha256})
    )


def cmd_journal(args: argparse.Namespace) -> int:
    print(json.dumps(_store(args).operations()[-args.limit :], indent=2, default=str))
    return 0


def cmd_database(args: argparse.Namespace) -> int:
    from deploy.agent.database import local_command

    return _show(local_command(_executor(args), args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m deploy.agent")
    parser.add_argument("--root", required=True, help="the deployment root (DEPLOY_ROOT)")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init", help="create this machine's agent store")
    init.add_argument("--role", required=True, choices=("control-plane", "host"))
    init.add_argument("--name")
    init.add_argument("--control-plane-url")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    register = commands.add_parser("register", help="join a control plane with an enrollment token")
    register.add_argument("--token")
    register.add_argument("--control-plane-url")
    register.set_defaults(func=cmd_register)

    run = commands.add_parser("run", help="pull and execute intents")
    run.add_argument("--interval", type=float, default=10.0)
    run.add_argument("--once", action="store_true")
    run.set_defaults(func=cmd_run)

    commands.add_parser("status", help="what this machine runs").set_defaults(func=cmd_status)

    apply = commands.add_parser("apply", help="apply configuration changes locally")
    apply.add_argument("assignments", nargs="*", metavar="NAME=VALUE")
    apply.add_argument(
        "--secret", action="append", metavar="NAME", help="prompt for a secret value"
    )
    apply.set_defaults(func=cmd_apply)

    rollback = commands.add_parser("rollback", help="re-apply a stored configuration version")
    rollback.add_argument("--rev", type=int, required=True)
    rollback.set_defaults(func=cmd_rollback)

    restart = commands.add_parser("restart", help="recreate services")
    restart.add_argument("services", nargs="+")
    restart.set_defaults(func=cmd_restart)

    release = commands.add_parser("release", help="verified releases")
    release_commands = release.add_subparsers(dest="release_command", required=True)
    imported = release_commands.add_parser("import")
    imported.add_argument("--manifest", required=True)
    imported.add_argument("--recipe", required=True)
    imported.add_argument("--sha256", help="the pinned SHA-256 of the manifest's bytes")
    release_commands.add_parser("list")
    phase = release_commands.add_parser("phase")
    phase.add_argument("--sha256", required=True)
    phase.add_argument(
        "--phase", required=True, choices=("prepare", "prepare-database", "activate", "abandon")
    )
    release.set_defaults(func=cmd_release)

    kata = commands.add_parser("kata", help="install a reviewed Kata version")
    kata.add_argument("--version", required=True)
    kata.add_argument("--sha256", required=True)
    kata.set_defaults(func=cmd_kata)

    journal = commands.add_parser("journal", help="recent operations")
    journal.add_argument("--limit", type=int, default=20)
    journal.set_defaults(func=cmd_journal)

    database = commands.add_parser("database", help="database recovery commands")
    database.add_argument("database_command", choices=("status", "unfence", "resume"))
    database.add_argument("--operation-id", default="")
    database.set_defaults(func=cmd_database)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (StoreError, releases.ReleaseError, ControlPlaneUnavailable) as exc:
        print(f"deploy agent: {exc}", file=sys.stderr)
        return 1


def new_operation_id() -> str:
    """A fresh journal id."""
    return uuid.uuid4().hex


__all__ = ["build_parser", "main"]
