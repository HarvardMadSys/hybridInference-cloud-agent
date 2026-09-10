"""Generate the storage port Protocols from the Postgres implementations.

Hand-transcribing 108 signatures is how a port quietly stops matching its
implementation. This derives each Protocol from the emitted repository, so the
parameter names, keyword-only markers, defaults and return types are the ones
the implementation actually has, and ``bootstrap`` can assert the match at
construction time.

Usage:
    python tools/generate_ports.py            # report
    python tools/generate_ports.py --write    # emit cloud_agent/ports/
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tools"))
import split_store as s

PORTS = REPO / "cloud_agent" / "ports"
REPOS = REPO / "cloud_agent" / "storage" / "repositories"

#: The docstring that heads each port module.
PORT_DOCSTRINGS: dict[str, str] = {
    "ConversationCommands": "Creating conversations and changing their metadata.",
    "ConversationQueries": "User-scoped reads over conversations, jobs and projects.",
    "AttemptCommands": "Claim, lease, cancellation and the atomic end of a turn.",
    "ExecutionQueries": "Observation reads over events, attempts and usage inputs.",
    "SandboxRepository": "Sandbox activation, admission, settlement and reconciliation.",
    "ResidencyRepository": "Residency lifecycles that outlive one attempt.",
    "SessionForkRepository": "Fork preparation, leases, receipts and recovery.",
    "SnapshotRepository": "Snapshot capture, publication and base import.",
    "HostPoolRepository": "Enrollment, heartbeat, capacity, placement and host reads.",
    "SourceControlRepository": "Repository grants, OAuth state and GitLab connections.",
    "McpConnectionRepository": "Per-user MCP connection storage.",
    "GrantRepository": "Attempt and residency grant records and revocation queries.",
    "CredentialQueries": "The live credential fence the MCP relay resolves per call.",
    "LedgerRepository": "Usage snapshot reads, refresh queueing, leases and commits.",
    "AuditRepository": "Durable admin audit records.",
}

HEADER = '''"""{doc}

A Protocol rather than an abstract base class: the implementation is never
imported here, so a caller depends on the shape while the storage layer depends
on this file, and neither depends on the other.
"""

from __future__ import annotations

from typing import Any, Protocol

'''


def repository_methods(module: str) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Return the public methods of one emitted repository, in file order."""
    path = REPOS / f"{module}.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # Top level only: a helper module imported as ``_attempts`` would otherwise
    # make its own module-level function names look like repository methods.
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            return {
                member.name: member
                for member in node.body
                if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not member.name.startswith("_")
            }
    raise AssertionError(f"no repository class in {path}")


def _render_arg(arg: ast.arg, default: ast.expr | None) -> str:
    """Render one parameter, keeping its annotation and default."""
    text = arg.arg
    if arg.annotation is not None:
        text = f"{text}: {ast.unparse(arg.annotation)}"
    if default is not None:
        text = f"{text} = {ast.unparse(default)}"
    return text


def stub(method: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render one Protocol method: the real signature, then ``...``."""
    args = method.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults)) + list(
        args.defaults
    )
    if not positional or positional[0].arg != "self":
        raise AssertionError(f"{method.name} does not take self as its first parameter")

    # ``self`` is kept, without an annotation. Dropping it looks tidier and is
    # wrong twice over: a method that takes only keyword-only arguments would
    # then have no positional parameter at all, and a type checker reads the
    # ``*`` marker as making ``self`` keyword-only.
    parts = [_render_arg(arg, default) for arg, default in zip(positional, defaults, strict=True)]
    kwonly = [
        _render_arg(arg, default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
    ]
    if args.vararg is not None:
        parts.append("*" + args.vararg.arg)
    elif kwonly:
        # The implementation wrote ``*, a, b`` after ``self``. Dropping ``self``
        # would leave ``a`` positional, so the marker is re-emitted: a caller
        # that passes these by keyword must stay unable to pass them by position.
        parts.append("*")
    parts += kwonly
    if args.kwarg is not None:
        parts.append("**" + args.kwarg.arg)

    returns = f" -> {ast.unparse(method.returns)}" if method.returns is not None else ""
    prefix = "async def" if isinstance(method, ast.AsyncFunctionDef) else "def"
    docstring = (ast.get_docstring(method) or "").split("\n")[0].strip()
    lines = [f"    {prefix} {method.name}("]
    lines += [f"        {part}," for part in parts]
    lines.append(f"    ){returns}:")
    if docstring:
        lines.append(f'        """{docstring}"""')
    lines.append("        ...")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Report the ports, or write them with ``--write``."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="emit cloud_agent/ports/")
    args = parser.parse_args(argv)

    by_port: dict[str, set[str]] = {}
    module_for_port: dict[str, str] = {}
    for name, target in s.APPENDIX.items():
        by_port.setdefault(target.port, set()).add(name)
        module_for_port[target.port] = target.module

    emitted: dict[pathlib.Path, str] = {}
    total = 0
    for port in sorted(by_port):
        module = module_for_port[port]
        methods = repository_methods(module)
        appendix_names = by_port[port]
        assert set(methods) == appendix_names, (
            f"{port}: implementation and appendix disagree: {sorted(set(methods) ^ appendix_names)}"
        )
        total += len(methods)
        body = "\n\n".join(stub(method) for method in methods.values())
        doc = PORT_DOCSTRINGS[port]
        emitted[PORTS / f"{module}.py"] = (
            HEADER.format(doc=doc)
            + f"class {port}(Protocol):\n"
            + f'    """{doc}"""\n\n'
            + body
            + "\n"
        )
        print(f"  {port:26s} {len(methods):3d} methods -> ports/{module}.py")

    emitted[PORTS / "__init__.py"] = (
        '"""Domain capability contracts.\n\n'
        "Each module here describes one capability. A business caller depends on the\n"
        "port it needs and never on a pool, a connection or a concrete implementation.\n"
        '"""\n'
    )
    print(f"  {'TOTAL':26s} {total:3d} methods")

    if args.write:
        for path, text in sorted(emitted.items()):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            print(f"wrote {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
