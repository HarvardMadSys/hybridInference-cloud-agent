"""Mechanically split cloud_agent/storage/store.py into ports and repositories.

This tool exists so the one-step persistence refactor moves code instead of
rewriting it. Every method body is sliced out of the original file with
``ast`` line/column information and emitted verbatim, so the SQL, the bind
parameter order, the branch order and the lock order cannot drift. The only
edits applied to a moved body are:

1. module-level names are requalified to their new home, and
2. ``self.<private helper>`` becomes a call to the helper's new module.

Every such edit is recorded in ``build/requalified-<module>.txt`` so a reviewer
can check the diff one rename at a time instead of trusting a 7,000-line move.

Usage:
    python tools/split_store.py            # report the split
    python tools/split_store.py --write    # emit the new modules
"""

from __future__ import annotations

import argparse
import ast
import pathlib
import sys
import textwrap
from typing import NamedTuple

REPO = pathlib.Path(__file__).resolve().parent.parent
STORE = REPO / "cloud_agent" / "storage" / "store.py"
DOMAIN = REPO / "cloud_agent" / "domain"
PORTS = REPO / "cloud_agent" / "ports"
REPOS = REPO / "cloud_agent" / "storage" / "repositories"
SQLDIR = REPO / "cloud_agent" / "storage" / "_sql"
#: The attribution report is committed: it is the artifact a reviewer checks
#: the split against, so it must not live in an ignored build directory.
REPORT = REPO / "docs" / "storage"
REQUALIFIED = REPORT / "method-attribution.txt"


class Method(NamedTuple):
    """One method of the old store, located in its source file."""

    name: str
    node: ast.AST
    is_async: bool


class Target(NamedTuple):
    """The destination of one old operation."""

    port: str
    module: str
    class_name: str


# Appendix A of docs/specs/2026-09-10-storage-one-step-redesign.zh-CN.md.
# Every one of the 108 business operations appears exactly once. ``initialize``
# is deliberately absent: it is retained as a no-op, because
# tests/unit/test_migration_baseline.py pins it and this repository's rules
# forbid weakening a test to make a refactor land.
_GROUPS: dict[str, tuple[str, str, list[str]]] = {
    "ConversationCommands": (
        "conversations",
        "PostgresConversationCommands",
        [
            "create_job",
            "create_follow_up",
            "set_sessions_archived",
            "delete_sessions",
            "set_thread_archived",
            "set_thread_title",
            "set_generated_thread_title",
            "set_thread_pinned",
        ],
    ),
    "ConversationQueries": (
        "conversation_queries",
        "PostgresConversationQueries",
        [
            "get_thread_for_job",
            "get_job",
            "list_jobs",
            "list_conversations",
            "list_projects",
            "list_sessions",
        ],
    ),
    "AttemptCommands": (
        "attempts",
        "PostgresAttemptCommands",
        [
            "claim_job",
            "release_claim",
            "heartbeat",
            "transition",
            "request_cancel",
            "reap_expired",
            "append_event",
            "record_continuity_mode",
            "record_attempt_resources",
        ],
    ),
    "ExecutionQueries": (
        "execution_queries",
        "PostgresExecutionQueries",
        [
            "list_thread_events_after",
            "thread_stats_inputs",
            "job_usage_inputs",
            "job_attempts_telemetry",
            "job_turn_telemetry",
            "list_events_after",
        ],
    ),
    "SandboxRepository": (
        "sandboxes",
        "PostgresSandboxRepository",
        [
            "thread_workspace_ready",
            "fence_terminal_workspace",
            "list_terminal_resumes_pending",
            "mark_terminal_resume_complete",
            "reserve_thread_sandbox",
            "mark_thread_sandbox_running",
            "record_thread_sandbox_process",
            "record_shared_thread_residency",
            "admit_thread_sandbox_turn",
            "settle_thread_sandbox",
            "reconcile_thread_sandbox_observation",
            "due_thread_sandbox_stop",
            "abandon_thread_sandbox",
            "thread_sandbox",
        ],
    ),
    "ResidencyRepository": (
        "residencies",
        "PostgresResidencyRepository",
        [
            "establish_residency",
            "heartbeat_residency",
            "release_residency",
            "release_shared_residency_if_quiescent",
            "resident_host",
            "reap_expired_residencies",
        ],
    ),
    "SessionForkRepository": (
        "session_forks",
        "PostgresSessionForkRepository",
        [
            "prepare_session_fork",
            "establish_session_fork_residencies",
            "claim_session_fork_materialization",
            "release_session_fork_materialization",
            "record_session_fork_materialized",
            "finalize_session_fork",
            "reap_stalled_session_forks",
        ],
    ),
    "SnapshotRepository": (
        "snapshots",
        "PostgresSnapshotRepository",
        [
            "claim_due_sandbox_snapshot",
            "record_sandbox_snapshot_capture",
            "defer_sandbox_snapshot_capture",
            "reap_stalled_sandbox_snapshot_captures",
            "claim_for_publish",
            "record_snapshot_publish",
            "fail_snapshot_publish",
            "pending_snapshot_base_import",
            "complete_snapshot_base_import",
            "record_snapshot_base_import_failure",
            "reap_stalled_snapshot_publishes",
        ],
    ),
    "HostPoolRepository": (
        "host_pool",
        "PostgresHostPoolRepository",
        [
            "issue_enroll_token",
            "consume_enroll_token",
            "get_host",
            "register_host",
            "authenticate_host",
            "record_host_heartbeat",
            "set_host_status",
            "remove_host",
            "reap_offline_hosts",
            "host_for_attempt",
            "host_for_job",
            "active_runner_host",
            "runner_pool",
            "deployment_runner_pool",
            "list_runner_hosts",
            "set_runtime_version",
            "set_active_runner_host",
            "forget_runner_host",
        ],
    ),
    "SourceControlRepository": (
        "source_control",
        "PostgresSourceControlRepository",
        [
            "record_repo_grant",
            "list_repo_grants",
            "revoke_repo_grant",
            "create_oauth_state",
            "consume_oauth_state",
            "upsert_gitlab_connection",
            "get_gitlab_connection",
            "update_gitlab_tokens",
            "delete_gitlab_connection",
        ],
    ),
    "McpConnectionRepository": (
        "mcp",
        "PostgresMcpConnectionRepository",
        ["list_mcp_connections", "upsert_mcp_connection", "delete_mcp_connection"],
    ),
    "GrantRepository": (
        "grants",
        "PostgresGrantRepository",
        [
            "record_attempt_grant",
            "list_renewable_grants",
            "take_revocable_grants",
            "link_residency_grant",
        ],
    ),
    "CredentialQueries": (
        "credential_queries",
        "PostgresCredentialQueries",
        ["resolve_model_credential"],
    ),
    "LedgerRepository": (
        "ledger",
        "PostgresLedgerRepository",
        [
            "ledger_rows_for_job",
            "ledger_rows_for_conversation",
            "queue_job_ledger_refresh",
            "claim_due_ledger_refresh",
            "complete_ledger_refresh",
        ],
    ),
    "AuditRepository": ("audit", "PostgresAuditRepository", ["record_admin_action"]),
}

APPENDIX: dict[str, Target] = {
    name: Target(port, module, class_name)
    for port, (module, class_name, names) in _GROUPS.items()
    for name in names
}

# Module-level names of the old store and where each one moves. The engine
# requalifies every use of these names, and nothing else. A bare module name is
# the alias a call site uses; the emitter turns it into the matching import.
GLOBAL_MOVES: dict[str, str] = {
    "QUEUED": "_execution",
    "WAITING": "_execution",
    "RUNNING": "_execution",
    "PUBLISHING": "_execution",
    "SUCCEEDED": "_execution",
    "FAILED": "_execution",
    "CANCELLED": "_execution",
    "TERMINAL_STATES": "_execution",
    "ATTEMPT_RUNNING": "_execution",
    "ATTEMPT_SUPERSEDED": "_execution",
    "ATTEMPT_FINISHED": "_execution",
    "EVENT_ATTEMPT_SUPERSEDED": "_execution",
    "EVENT_ATTEMPT_ABORTED": "_execution",
    "ATTEMPT_ABORTED": "_execution",
    "resolve_runtime_version": "_runtime_policy",
    "_new_job_id": "_ids",
    "_new_thread_id": "_ids",
    "_new_sandbox_id": "_ids",
    "_new_session_fork_id": "_ids",
    "_load_json": "_rows",
    "_sandbox_row_to_dict": "_rows",
    "_job_row_to_dict": "_rows",
    "_session_query": "_sessions",
    "_session_cursor_scope": "_sessions",
    "_decode_session_cursor": "_sessions",
    "_encode_session_cursor": "_sessions",
    "_decode_conversation_cursor": "_sessions",
    "_SESSION_DELETE_BLOCK_SQL": "_sessions",
    "_JOB_COLUMNS": "_rows",
    "_QUALIFIED_JOB_COLUMNS": "_rows",
    "_HOST_COLUMN_NAMES": "_hosts",
    "_HOST_COLUMNS": "_hosts",
    "_QUALIFIED_HOST_COLUMNS": "_hosts",
    "_THREAD_SANDBOX_SOURCE": "_rows",
    "_LEDGER_WINDOW_UNTIL_SQL": "_ledger",
    "_NATIVE_FORK_CAPABILITY_JSON": "_session_forks",
}

# Private methods of the old store that become module-level functions. The
# value is the module alias they are called through; ``None`` means the helper
# stays a method of one repository because it owns its own transaction.
HELPER_MOVES: dict[str, str | None] = {
    "_insert_event": "_events",
    "_release_host_slot": "_host_slots",
    "_advance_waiting_follow_ups": "_followups",
    "_fail_unrunnable_follow_ups": "_followups",
    "_capture_attempt_ledger": "_ledger",
    "_attempts_spent": "_attempts",
    "_session_fork_details": "_session_forks",
    "_reserve_shared_thread_sandbox": "_sandboxes",
    "_host_row_to_dict": "_hosts",
    "_reconcile_shared_sandbox_observation": None,
}

# Helpers that lose their ``self`` parameter when they become functions. The
# helpers that only ever took ``conn`` are unaffected.
DROP_SELF: set[str] = {
    "_insert_event",
    "_session_fork_details",
    "_reserve_shared_thread_sandbox",
}

# The name a moved callable gets in its new module. The leading underscore is
# dropped: ``_sql`` is private to infrastructure already, so a second marker
# would only add noise to every call site. Constants keep their name verbatim.
HELPER_RENAMES: dict[str, str] = {
    "_insert_event": "insert_event",
    "_release_host_slot": "release_host_slot",
    "_advance_waiting_follow_ups": "advance_waiting_follow_ups",
    "_fail_unrunnable_follow_ups": "fail_unrunnable_follow_ups",
    "_capture_attempt_ledger": "capture_attempt_ledger",
    "_attempts_spent": "attempts_spent",
    "_session_fork_details": "session_fork_details",
    "_reserve_shared_thread_sandbox": "reserve_shared_thread_sandbox",
    "_host_row_to_dict": "host_row_to_dict",
    # Module-level functions that keep their meaning but lose the marker.
    "_load_json": "load_json",
    "_sandbox_row_to_dict": "sandbox_row_to_dict",
    "_job_row_to_dict": "job_row_to_dict",
    "_session_query": "session_query",
    "_session_cursor_scope": "session_cursor_scope",
    "_decode_session_cursor": "decode_session_cursor",
    "_encode_session_cursor": "encode_session_cursor",
    "_decode_conversation_cursor": "decode_conversation_cursor",
}


#: For a named callable, the local name it has once emitted. Constants and
#: module-level values keep their own name.
def local_name(old: str) -> str:
    """Return the name a moved definition carries in its new module."""
    return HELPER_RENAMES.get(old, old)


#: Alias -> real module path under cloud_agent. The emitter resolves these into
#: imports, so ``_rows.load_json`` becomes ``from ..._sql import rows as _rows``.
SQL_ALIASES: dict[str, str] = {
    "_rows": "cloud_agent.storage._sql.rows",
    "_getters": "cloud_agent.storage._sql.getters",
    "_sessions": "cloud_agent.storage._sql.sessions",
    "_hosts": "cloud_agent.storage._sql.hosts",
    "_host_slots": "cloud_agent.storage._sql.host_slots",
    "_sandboxes": "cloud_agent.storage._sql.sandboxes",
    "_ledger": "cloud_agent.storage._sql.ledger",
    "_session_forks": "cloud_agent.storage._sql.session_forks",
    "_events": "cloud_agent.storage._sql.events",
    "_followups": "cloud_agent.storage._sql.followups",
    "_attempts": "cloud_agent.storage._sql.attempts",
}

#: The non-SQL aliases, resolved the same way.
DOMAIN_ALIASES: dict[str, str] = {
    "_execution": "cloud_agent.domain.execution",
    "_runtime_policy": "cloud_agent.domain.runtime_policy",
    "_ids": "cloud_agent.domain.ids",
}

ALIASES: dict[str, str] = {**SQL_ALIASES, **DOMAIN_ALIASES}

# A cross-port read that moves into _sql because two repositories need the same
# projection. Maps the old public method to its new module-level function.
SHARED_READS: dict[str, str] = {"get_job": "_getters"}

#: Which _sql module each moved helper or module-level name is emitted into.
#: Derived from the alias so a wrong alias cannot silently land in the wrong
#: file.
MODULE_PATHS: dict[str, str] = {
    module: f"cloud_agent.storage._sql.{module}" for module in SQL_ALIASES.values()
}
MODULE_PATHS = {path.rsplit(".", 1)[-1]: path for path in MODULE_PATHS.values()}

SQL_MODULES: dict[str, list[str]] = {
    "rows": [
        "_load_json",
        "_sandbox_row_to_dict",
        "_job_row_to_dict",
        "_JOB_COLUMNS",
        "_QUALIFIED_JOB_COLUMNS",
        "_THREAD_SANDBOX_SOURCE",
    ],
    "getters": ["get_job"],
    "sessions": [
        "_session_query",
        "_session_cursor_scope",
        "_decode_session_cursor",
        "_encode_session_cursor",
        "_decode_conversation_cursor",
        "_SESSION_DELETE_BLOCK_SQL",
    ],
    "hosts": [
        "_HOST_COLUMN_NAMES",
        "_HOST_COLUMNS",
        "_QUALIFIED_HOST_COLUMNS",
        "_host_row_to_dict",
    ],
    "host_slots": ["_release_host_slot"],
    "sandboxes": ["_reserve_shared_thread_sandbox"],
    "ledger": ["_LEDGER_WINDOW_UNTIL_SQL", "_capture_attempt_ledger"],
    "session_forks": ["_NATIVE_FORK_CAPABILITY_JSON", "_session_fork_details"],
    "events": ["_insert_event"],
    "followups": ["_advance_waiting_follow_ups", "_fail_unrunnable_follow_ups"],
    "attempts": ["_attempts_spent"],
}

MODULE_HEADER = '''"""{title}"""

from __future__ import annotations

from typing import Any

from cloud_agent.storage.repositories._base import PostgresRepository


class {class_name}(PostgresRepository):
    """{doc}"""
'''


def load_store() -> tuple[str, ast.Module, ast.ClassDef]:
    """Parse the old store and return its source, tree and class node."""
    source = STORE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    cls = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AgentJobStore"
    )
    return source, tree, cls


def slice_node(source: str, node: ast.AST) -> str:
    """Return the exact source text of ``node``, dedented and decorators included.

    Dedenting is the only whitespace change the engine makes. It is needed
    because a method lifted out of a class body must parse as a top-level
    statement before it can be requalified.
    """
    lines = source.splitlines(keepends=True)
    start = node.lineno - 1
    decorators = getattr(node, "decorator_list", None)
    if decorators:
        start = min(dec.lineno for dec in decorators) - 1
    raw = "".join(lines[start : node.end_lineno])
    return textwrap.dedent(raw).rstrip("\n")


def inventory(source: str, cls: ast.ClassDef) -> tuple[dict[str, Method], dict[str, Method]]:
    """Split the class body into public operations and private helpers."""
    public: dict[str, Method] = {}
    private: dict[str, Method] = {}
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name == "__init__":
            continue
        entry = Method(node.name, node, isinstance(node, ast.AsyncFunctionDef))
        if node.name.startswith("_"):
            private[node.name] = entry
        else:
            public[node.name] = entry
    return public, private


# --------------------------------------------------------------------------
# Requalification
# --------------------------------------------------------------------------


class Edit(NamedTuple):
    """One text substitution applied to a moved body."""

    lineno: int
    col: int
    end_col: int
    old: str
    new: str


def requalify(body: str, *, owner: str | None = None) -> tuple[str, list[Edit]]:
    """Requalify module-level names and moved helper calls in one body.

    The edits are computed from the parsed fragment, so a name that merely
    appears inside a SQL string is never touched. Three classes of edit exist:

    1. a moved module-level value is prefixed with its new module alias,
    2. ``self.<helper>(...)`` becomes ``<alias>.<helper>(...)``, and
    3. a top-level ``def`` of a moved helper is renamed to its new local name.
    """
    tree = ast.parse(body)
    lines = body.splitlines()
    edits: list[Edit] = []

    # The def/assign statement that introduces a moved name, so its own name is
    # renamed locally instead of being read as a reference to another module.
    # Keyed by node identity of the *name* node, which is what ``ast.walk``
    # yields: keying on the statement would never match a Name.
    definitions: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    definitions[target.id] = target

    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in GLOBAL_MOVES:
            if definitions.get(node.id) is node:
                # This is the moved definition itself.
                new = local_name(node.id)
                if new != node.id:
                    edits.append(
                        Edit(
                            node.lineno,
                            node.col_offset,
                            node.end_col_offset or node.col_offset,
                            node.id,
                            new,
                        )
                    )
                continue
            alias = GLOBAL_MOVES[node.id]
            # A module that owns the name refers to it directly rather than
            # through an import of itself. ``owner`` is a full module path, so
            # the repository named ``ledger`` is not confused with the SQL
            # helper module also named ``ledger``.
            if owner is not None and ALIASES.get(alias) == owner:
                new = local_name(node.id)
            else:
                new = f"{alias}.{local_name(node.id)}"
            edits.append(
                Edit(
                    node.lineno,
                    node.col_offset,
                    node.end_col_offset or node.col_offset,
                    node.id,
                    new,
                )
            )
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in HELPER_MOVES
        ):
            alias = HELPER_MOVES[node.attr]
            if alias is not None:
                edits.append(
                    Edit(
                        node.value.lineno,
                        node.value.col_offset,
                        node.end_col_offset or node.col_offset,
                        f"self.{node.attr}",
                        f"{alias}.{HELPER_RENAMES[node.attr]}",
                    )
                )
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in SHARED_READS
        ):
            edits.append(
                Edit(
                    node.value.lineno,
                    node.value.col_offset,
                    node.end_col_offset or node.col_offset,
                    f"self.{node.attr}",
                    f"{SHARED_READS[node.attr]}.{node.attr}_projection",
                )
            )

    # A moved helper's own definition is renamed at the point of definition.
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        new_name = local_name(node.name)
        if new_name == node.name:
            continue
        prefix = len("async def ") if isinstance(node, ast.AsyncFunctionDef) else len("def ")
        edits.append(
            Edit(
                node.lineno,
                node.col_offset + prefix,
                node.col_offset + prefix + len(node.name),
                node.name,
                new_name,
            )
        )

    if not edits:
        return body, []

    # Apply right-to-left inside each line so earlier columns stay valid.
    out = list(lines)
    for edit in sorted(edits, key=lambda e: (e.lineno, e.col), reverse=True):
        line = out[edit.lineno - 1]
        segment = line[edit.col : edit.end_col]
        if segment != edit.old:
            raise AssertionError(
                f"requalify mismatch on line {edit.lineno}: {segment!r} != {edit.old!r}"
            )
        out[edit.lineno - 1] = line[: edit.col] + edit.new + line[edit.end_col :]
    return "\n".join(out), sorted(edits, key=lambda e: (e.lineno, e.col))


def module_preamble(body: str) -> str:
    """Return the module-level definitions a moved body assumes exist."""
    needed = {node.id for node in ast.walk(ast.parse(body)) if isinstance(node, ast.Name)}
    parts: list[str] = []
    if "logger" in needed:
        parts.append(
            "from cloud_agent.logging_utils import get_logger\n\nlogger = get_logger(__name__)"
        )
    return "\n\n".join(parts)


def names_in(body: str) -> set[str]:
    """Return every module-level name the body references."""
    return {
        node.id
        for node in ast.walk(ast.parse(body))
        if isinstance(node, ast.Name) and node.id in GLOBAL_MOVES
    }


def helper_calls_in(body: str) -> set[str]:
    """Return every moved helper the body calls, as an old helper name."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(body)):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and node.attr in HELPER_MOVES
        ):
            found.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in HELPER_MOVES:
            found.add(node.id)
    return found


def build_report(
    source: str,
    cls: ast.ClassDef,
    public: dict[str, Method],
    private: dict[str, Method],
) -> str:
    """Describe every requalification the writers would apply."""
    lines: list[str] = []
    lines.append("== module-level names requalified ==")
    for old, new in sorted(GLOBAL_MOVES.items(), key=lambda kv: kv[1]):
        users = [
            name
            for name, entry in list(public.items()) + list(private.items())
            if old in names_in(slice_node(source, entry.node))
        ]
        lines.append(f"  {old:32s} -> {new:22s} used by {len(users):2d} methods")

    lines.append("")
    lines.append("== helpers becoming module-level functions ==")
    for old, alias in sorted(HELPER_MOVES.items()):
        entry = private.get(old)
        if entry is None:
            lines.append(f"  {old:34s} MISSING")
            continue
        callers = [
            name
            for name, candidate in list(public.items()) + list(private.items())
            if old in helper_calls_in(slice_node(source, candidate.node))
        ]
        where = alias if alias else "stays a method of its repository"
        lines.append(f"  {old:34s} -> {where:22s} callers: {len(callers)}")

    lines.append("")
    lines.append("== cross-port reads moved into _sql ==")
    for old, alias in sorted(SHARED_READS.items()):
        entry = public.get(old)
        lines.append(f"  {old:34s} -> {alias}.{old}_projection")

    lines.append("")
    lines.append("== per-repository method counts ==")
    counts: dict[str, int] = {}
    for target in APPENDIX.values():
        counts[target.module] = counts.get(target.module, 0) + 1
    for module in sorted(counts):
        lines.append(f"  {module:26s} {counts[module]:3d}")
    lines.append(f"  {'TOTAL':26s} {sum(counts.values()):3d}")
    return "\n".join(lines)


def write_report(report: str) -> None:
    """Persist the requalification report next to the build artifacts."""
    REPORT.mkdir(parents=True, exist_ok=True)
    REQUALIFIED.write_text(report + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Import inference
# --------------------------------------------------------------------------

#: Every name the old store imports or defines at module level, mapped to what
#: an emitted module must do to see it again. ``None`` means "the emitted tree
#: defines this itself"; a tuple means "import NAME from MODULE".
IMPORT_SPEC: dict[str, tuple[str, str] | None] = {
    "Any": ("typing", "Any"),
    "datetime": ("datetime", "datetime"),
    "base64": ("base64", "__module__"),
    "binascii": ("binascii", "__module__"),
    "hashlib": ("hashlib", "__module__"),
    "json": ("json", "__module__"),
    "secrets": ("secrets", "__module__"),
    "KATA_RUNTIME": ("cloud_agent.execution_profiles", "KATA_RUNTIME"),
    "ExecutionProfile": ("cloud_agent.execution_profiles", "ExecutionProfile"),
    "compatible_shared_sandbox_followup_attested": (
        "cloud_agent.execution_profiles",
        "compatible_shared_sandbox_followup_attested",
    ),
    "default_execution_profile": ("cloud_agent.execution_profiles", "default_execution_profile"),
    "shared_sandbox_claim_attested": (
        "cloud_agent.execution_profiles",
        "shared_sandbox_claim_attested",
    ),
    "CLAIMABLE_STATUSES": ("cloud_agent.host_pool", "CLAIMABLE_STATUSES"),
    "HOST_DISABLED": ("cloud_agent.host_pool", "HOST_DISABLED"),
    "HOST_ENROLLING": ("cloud_agent.host_pool", "HOST_ENROLLING"),
    "HOST_OFFLINE": ("cloud_agent.host_pool", "HOST_OFFLINE"),
    "HOST_ONLINE": ("cloud_agent.host_pool", "HOST_ONLINE"),
    "normalize_capabilities": ("cloud_agent.host_pool", "normalize_capabilities"),
    "transition_allowed": ("cloud_agent.host_pool", "transition_allowed"),
    "get_logger": ("cloud_agent.logging_utils", "get_logger"),
    "fallback_title": ("cloud_agent.task_titles", "fallback_title"),
    "NATIVE_FORK_AT_ANCHOR_CAPABILITY": (
        "cloud_agent_host.shared_protocol",
        "NATIVE_FORK_AT_ANCHOR_CAPABILITY",
    ),
    # Defined by the emitted tree rather than imported.
    "logger": None,
    "resolve_runtime_version": None,
    "_new_job_id": None,
    "_new_thread_id": None,
    "_new_sandbox_id": None,
    "_new_session_fork_id": None,
    "_load_json": None,
    "_sandbox_row_to_dict": None,
    "_job_row_to_dict": None,
    "_session_query": None,
    "_session_cursor_scope": None,
    "_decode_session_cursor": None,
    "_encode_session_cursor": None,
    "_decode_conversation_cursor": None,
    "_SESSION_DELETE_BLOCK_SQL": None,
    "_JOB_COLUMNS": None,
    "_QUALIFIED_JOB_COLUMNS": None,
    "_HOST_COLUMN_NAMES": None,
    "_HOST_COLUMNS": None,
    "_QUALIFIED_HOST_COLUMNS": None,
    "_THREAD_SANDBOX_SOURCE": None,
    "_LEDGER_WINDOW_UNTIL_SQL": None,
    "_NATIVE_FORK_CAPABILITY_JSON": None,
}

STORED_IMPORTS: dict[str, tuple[str, str]] = {
    name: spec for name, spec in IMPORT_SPEC.items() if spec is not None
}

#: Names that need the ``asyncpg`` module rather than an imported symbol. The
#: store guarded these behind TYPE_CHECKING, so an emitted module that uses one
#: must carry the same guard.
ASYNCPG_USERS = ("asyncpg",)

_STDLIB_ROOTS = {
    "typing",
    "datetime",
    "json",
    "secrets",
    "base64",
    "binascii",
    "hashlib",
    "textwrap",
    "collections",
    "contextlib",
    "itertools",
    "math",
}


def imported_names(body: str, owned: set[str]) -> dict[str, tuple[str, str]]:
    """Return the imports ``body`` needs, excluding what ``owned`` defines."""
    needed: dict[str, tuple[str, str]] = {}
    for node in ast.walk(ast.parse(body)):
        name = None
        if isinstance(node, ast.Name):
            name = node.id
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            # ``json.dumps`` needs the ``json`` module in scope, not ``dumps``.
            name = node.value.id
        if name and name in STORED_IMPORTS and name not in owned:
            needed[name] = STORED_IMPORTS[name]
    return needed


def render_imports(imports: dict[str, tuple[str, str]]) -> str:
    """Render an import block grouped stdlib / third-party / first-party."""
    buckets: dict[str, list[str]] = {"std": [], "third": [], "first": []}
    for name, (module, _attr) in sorted(imports.items()):
        if _attr == "__module__":
            buckets["std"].append(f"import {module}")
            continue
        root = module.split(".")[0]
        if root in _STDLIB_ROOTS:
            buckets["std"].append(f"from {module} import {name}")
        elif root.startswith("cloud_agent"):
            buckets["first"].append(f"from {module} import {name}")
        else:
            buckets["third"].append(f"from {module} import {name}")
    blocks = ["\n".join(sorted(buckets[key])) for key in ("std", "third", "first")]
    return "\n\n".join(block for block in blocks if block)


def emit_domain(source: str, tree: ast.Module) -> dict[pathlib.Path, str]:
    """Emit cloud_agent/domain/**: business values and pure helpers."""
    lines = source.splitlines(keepends=True)

    def constants(*names: str) -> str:
        """Return the verbatim source of the named module-level constants."""
        chunks: list[str] = []
        seen: set[str] = set()
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in names and target.id not in seen:
                    seen.add(target.id)
                    chunks.append("".join(lines[node.lineno - 1 : node.end_lineno]).rstrip("\n"))
        missing = sorted(set(names) - seen)
        assert not missing, f"constants not found in the store: {missing}"
        return "\n".join(chunks)

    def function(name: str) -> str:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return "".join(lines[node.lineno - 1 : node.end_lineno]).rstrip("\n")
        raise AssertionError(f"module-level function not found: {name}")

    statuses = constants(
        "QUEUED",
        "WAITING",
        "RUNNING",
        "PUBLISHING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "TERMINAL_STATES",
    )
    attempts = constants(
        "ATTEMPT_RUNNING",
        "ATTEMPT_SUPERSEDED",
        "ATTEMPT_FINISHED",
        "EVENT_ATTEMPT_SUPERSEDED",
        "EVENT_ATTEMPT_ABORTED",
        "ATTEMPT_ABORTED",
    )

    execution = f'''"""Job, attempt and event vocabulary shared by every layer.

These live here rather than beside the SQL that writes them. Routes, schemas,
tests and the host contract all speak them, and none of those should import a
repository to name a state.
"""

from __future__ import annotations

{statuses}


# The two event names the store writes itself, and the attempt statuses that
# fence every write an attempt can lose.
{attempts}
'''

    policy_body = function("resolve_runtime_version")
    runtime_policy = f'''"""Which CLI version an attempt runs.

A thread is a native CLI session in its retained container, so the version it
started on is part of its identity. This is policy rather than persistence: it
decides from values a claim has already read.
"""

from __future__ import annotations

from typing import Any

{module_preamble(policy_body)}

{policy_body}
'''

    ids = f'''"""Identifier minting for the objects the control plane creates.

Ids are opaque and prefixed by kind, which is what makes a log line readable
without a join. They never encode meaning beyond that prefix.
"""

from __future__ import annotations

import secrets

{function("_new_job_id")}


{function("_new_thread_id")}


{function("_new_sandbox_id")}


{function("_new_session_fork_id")}
'''

    return {
        DOMAIN / "__init__.py": '"""Pure business values with no persistence dependency."""\n',
        DOMAIN / "execution.py": execution,
        DOMAIN / "runtime_policy.py": runtime_policy,
        DOMAIN / "ids.py": ids,
    }


def emit_sql(
    source: str, tree: ast.Module, public: dict[str, Method], private: dict[str, Method]
) -> dict[pathlib.Path, str]:
    """Emit cloud_agent/storage/_sql/**: conn-level helpers and shared projections.

    Every function here takes the caller's ``connection`` and never acquires
    its own. That is the property the design leans on: an operation's whole
    transaction stays in one method, and these helpers are only the pieces two
    of those methods share.
    """
    lines = source.splitlines(keepends=True)

    def top_level(name: str) -> str:
        """Return the exact source of a module-level definition or assignment."""
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return "".join(lines[node.lineno - 1 : node.end_lineno]).rstrip("\n")
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if name in targets:
                    return "".join(lines[node.lineno - 1 : node.end_lineno]).rstrip("\n")
        raise AssertionError(f"module-level definition not found: {name}")

    def method(name: str) -> str:
        entry = private.get(name) or public.get(name)
        assert entry is not None, f"method not found: {name}"
        return strip_decorators(slice_node(source, entry.node))

    def own(name: str) -> str:
        """Source of a moved definition, before requalification."""
        if name in private and HELPER_MOVES.get(name) is not None:
            return method(name)
        return top_level(name)

    def as_function(name: str) -> str:
        """A moved helper as a module-level function, without ``self``.

        The call sites already pass the connection positionally, so leaving
        ``self`` in the signature would bind the connection to it and fail on
        the first argument that matters.
        """
        body = own(name) if name not in private else strip_decorators(
            slice_node(source, private[name].node)
        )
        return drop_self_parameter(body) if name in DROP_SELF else body

    # Local names emitted into each module, needed so import inference does not
    # re-import a name the module defines itself.
    emitted: dict[pathlib.Path, str] = {}
    sql_text: dict[str, str] = {}

    for module, names in SQL_MODULES.items():
        bodies: list[str] = []
        if module == "getters":
            bodies.append(render_cross_port_read(source, public["get_job"].node))
        else:
            for name in names:
                bodies.append(as_function(name))
        joined = "\n\n\n".join(requalify(body, owner=MODULE_PATHS[module])[0] for body in bodies)
        sql_text[module] = joined

    for module, names in SQL_MODULES.items():
        local = {local_name(name) for name in names}
        body = sql_text[module]
        header = f'''"""{SQL_DOCSTRINGS[module]}"""

from __future__ import annotations

'''
        # ``getters`` is emitted from a repository method, so its own name is
        # not in ``local``; the other modules define what they list.
        direct = imported_names(body, owned=local)
        typing_names = sorted(name for name, spec in direct.items() if spec[0] == "typing")
        guard = ""
        if "asyncpg" in body:
            if "TYPE_CHECKING" not in typing_names:
                typing_names.append("TYPE_CHECKING")
                typing_names.sort()
            guard = "if TYPE_CHECKING:\n    import asyncpg"
        parts: list[str] = []
        if typing_names:
            parts.append(f"from typing import {', '.join(typing_names)}")
        rendered = render_imports({n: s_ for n, s_ in direct.items() if s_[0] != "typing"})
        if rendered:
            parts.append(rendered)
        aliases = alias_imports_for([body], exclude=MODULE_PATHS[module])
        if aliases:
            parts.append("\n".join(aliases))
        if guard:
            parts.append(guard)
        if parts:
            header += "\n\n".join(parts) + "\n\n\n"
        preamble = module_preamble(body)
        if preamble:
            header += preamble + "\n\n\n"
        emitted[SQLDIR / f"{module}.py"] = header + body + "\n"

    emitted[SQLDIR / "__init__.py"] = (
        '"""Connection-level SQL shared by more than one repository.\n\n'
        "Nothing here acquires a connection. A caller passes the one its\n"
        "transaction already holds, which is what keeps a cross-table operation a\n"
        "single commit.\n"
        '"""\n'
    )
    return emitted


def repository_path(module: str) -> str:
    """Return the dotted path of one emitted repository module."""
    return f"cloud_agent.storage.repositories.{module}"


def drop_self_parameter(body: str) -> str:
    """Remove ``self`` from a definition that is no longer a method.

    The removal is positional and touches nothing else: the trailing comma and
    the whitespace that followed ``self`` go with it, while the layout of the
    remaining parameters and of the whole body is preserved. ``ast.unparse``
    cannot be used instead, because it reflows a multi-line signature and would
    move the SQL literals in the body with it.
    """
    tree = ast.parse(body)
    node = tree.body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        raise AssertionError("drop_self_parameter needs a function definition")
    positional = [*node.args.posonlyargs, *node.args.args]
    if not positional or positional[0].arg != "self":
        raise AssertionError(f"{node.name} does not take self as its first parameter")

    target = positional[0]
    lines = body.splitlines(keepends=True)
    line = lines[target.lineno - 1]
    start, end = target.col_offset, target.end_col_offset or target.col_offset
    if line[start:end] != "self":
        raise AssertionError(f"column offsets did not locate self in {node.name}")
    if len(positional) > 1:
        # ``self`` is followed by its own separator, which goes with it.
        cursor = end
        if cursor < len(line) and line[cursor] == ",":
            cursor += 1
        while cursor < len(line) and line[cursor] in " \t":
            cursor += 1
        end = cursor
    else:
        # ``self`` was the only parameter, so the comma that follows the list
        # belongs to the call, not to ``self``; take the comma before it instead.
        if start > 0 and line[start - 1] == ",":
            start -= 1
    lines[target.lineno - 1] = line[:start] + line[end:]
    result = "".join(lines)
    parsed = ast.parse(result)
    remaining = [*parsed.body[0].args.args, *parsed.body[0].args.kwonlyargs]
    if any(argument.arg == "self" for argument in remaining):
        raise AssertionError(f"self survived the rewrite of {node.name}")
    return result


def strip_decorators(body: str) -> str:
    """Drop decorators from a definition that is no longer a class member.

    ``@staticmethod`` was right on ``_capture_attempt_ledger`` inside the store
    class and is a TypeError on a module-level function.
    """
    tree = ast.parse(body)
    node = tree.body[0]
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.decorator_list:
        return body
    lines = body.splitlines()
    cut = min(dec.lineno for dec in node.decorator_list) - 1
    keep = lines[:cut] + lines[node.lineno - 1 :]
    return "\n".join(keep)


def unindent_lines(text: str, width: int = 4) -> str:
    """Remove exactly ``width`` leading spaces from every non-blank line.

    ``textwrap.dedent`` cannot be used here: these bodies contain SQL string
    literals indented past the Python around them, so the common prefix is
    empty and dedent would silently do nothing.
    """
    out = []
    for line in text.splitlines():
        if line.strip() and line.startswith(" " * width):
            out.append(line[width:])
        else:
            out.append(line)
    return "\n".join(out)


def render_cross_port_read(source: str, node: ast.AST) -> str:
    """Turn the old ``get_job`` method into a conn-taking projection function.

    ``finalize_session_fork`` reads this projection *after* its commit, from a
    fresh connection, and the conversation read path uses it inside its own
    transaction. One function that takes the caller's connection serves both,
    so neither repository has to depend on the other.

    The SQL and the bind parameter are read off the parsed call and re-emitted,
    so the query text is carried over rather than retyped. The two closing lines
    are the whole of the new code.
    """
    lines = source.splitlines()
    func = node
    if not isinstance(func, ast.AsyncFunctionDef) or func.name != "get_job":
        raise AssertionError("get_job did not have the shape this rewrite expects")
    scopes = [item for item in func.body if isinstance(item, ast.AsyncWith)]
    if len(scopes) != 1:
        raise AssertionError("get_job did not have exactly one connection scope")
    fetch = scopes[0].body[0]
    if not isinstance(fetch, ast.Assign):
        raise AssertionError("get_job's first statement was not the projection fetch")
    # ``row = await conn.fetchrow(...)``: the call is wrapped in the await.
    awaited = fetch.value
    if not isinstance(awaited, ast.Await) or not isinstance(awaited.value, ast.Call):
        raise AssertionError("get_job's first statement was not an awaited fetch")
    call = awaited.value
    if len(call.args) < 2:
        raise AssertionError("get_job's projection fetch had no bind parameter")

    query = ast.unparse(call.args[0])
    parameter = ast.unparse(call.args[1])
    # Re-emit the query with the indentation the new function needs. The literal
    # itself is unchanged: only the leading whitespace of every line moves.
    query_lines = query.splitlines()
    indented = "\n".join(("        " + line) if line.strip() else line for line in query_lines)

    docstring = (ast.get_docstring(func) or "Fetch one job by id.").strip()
    rendered = (
        "async def get_job_projection(\n"
        "    conn: asyncpg.Connection, job_id: str\n"
        ") -> dict[str, Any] | None:\n"
        f'    """{docstring} Uses the connection the caller already holds."""\n'
        "    row = await conn.fetchrow(\n"
        f"{indented},\n"
        f"        {parameter},\n"
        "    )\n"
        "    return _rows.job_row_to_dict(row) if row else None"
    )
    # A rewrite that does not parse would ship a syntax error found at import
    # time, so the result is parsed here instead.
    ast.parse(rendered)
    del lines
    return rendered


def alias_imports_for(bodies: list[str], *, exclude: str | None = None) -> list[str]:
    """Return the import lines that make the requalified aliases resolvable.

    ``exclude`` is the full path of the module being emitted, so the repository
    named ``ledger`` is not mistaken for the SQL module also named ``ledger``.
    """
    used: set[str] = set()
    for body in bodies:
        for node in ast.walk(ast.parse(body)):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in ALIASES:
                    used.add(node.value.id)
            elif isinstance(node, ast.Name) and node.id in ALIASES:
                used.add(node.id)
    lines: list[str] = []
    for alias in sorted(used):
        if exclude is not None and ALIASES[alias] == exclude:
            continue
        lines.append(f"import {ALIASES[alias]} as {alias}")
    return lines


def emit_repositories(
    source: str,
    public: dict[str, Method],
    private: dict[str, Method],
) -> dict[pathlib.Path, str]:
    """Emit cloud_agent/storage/repositories/**: the Postgres implementations."""
    emitted: dict[pathlib.Path, str] = {}

    base = '''"""Shared construction for the Postgres repositories.

Every repository holds one pool and nothing else. A request-scoped connection
is never stored on the instance: two concurrent calls on one repository must
not be able to observe each other's transaction.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import asyncpg


class PostgresRepository:
    """Base for the storage implementations: one pool, no request state."""

    def __init__(self, pool: asyncpg.Pool) -> None:
        """Keep the shared pool; opening and closing it belongs to bootstrap."""
        self._pool = pool
'''
    emitted[REPOS / "_base.py"] = base

    class_names = {target.module: target.class_name for target in APPENDIX.values()}
    for module in sorted(class_names):
        class_name = class_names[module]
        names = [name for name, target in APPENDIX.items() if target.module == module]

        bodies = [
            requalify(slice_node(source, public[name].node), owner=repository_path(module))[0]
            for name in names
        ]

        # A helper that is still a method travels with the repository it serves.
        for old, alias in HELPER_MOVES.items():
            if alias is not None:
                continue
            if any(old in helper_calls_in(body) for body in bodies):
                bodies.append(
                    requalify(slice_node(source, private[old].node), owner=repository_path(module))[
                        0
                    ]
                )

        indented = "\n\n\n".join(textwrap.indent(body, "    ") for body in bodies)

        # Imports are entirely inferred. A header that hardcoded ``Any`` would
        # duplicate what the methods already pull in.
        direct = imported_names("\n\n".join(bodies), owned=set())
        import_parts: list[str] = []
        header = f'''"""{REPOSITORY_DOCSTRINGS[module]}"""

from __future__ import annotations

'''
        typing_names = sorted(name for name, spec in direct.items() if spec[0] == "typing")
        if "asyncpg" in indented:
            if "TYPE_CHECKING" not in typing_names:
                typing_names.append("TYPE_CHECKING")
                typing_names.sort()
            header += "if TYPE_CHECKING:\n    import asyncpg\n\n"
        if typing_names:
            import_parts.append(f"from typing import {', '.join(typing_names)}")
        rendered = render_imports({n: s_ for n, s_ in direct.items() if s_[0] != "typing"})
        if rendered:
            import_parts.append(rendered)
        aliases = alias_imports_for(bodies, exclude=repository_path(module))
        if aliases:
            import_parts.append("\n".join(aliases))
        import_parts.append("from cloud_agent.storage.repositories._base import PostgresRepository")
        header += "\n\n".join(import_parts) + "\n\n\n"
        preamble = module_preamble("\n\n".join(bodies))
        if preamble:
            header += preamble + "\n\n\n"

        doc = REPOSITORY_DOCSTRINGS[module]
        header += f"class {class_name}(PostgresRepository):\n"
        header += f'    """{doc}"""\n\n'
        emitted[REPOS / f"{module}.py"] = header + indented + "\n"

    emitted[REPOS / "__init__.py"] = (
        '"""Postgres implementations of the storage ports.\n\n'
        "Only bootstrap and infrastructure import these; a business caller depends on\n"
        "the port it needs, never on a pool.\n"
        '"""\n'
    )
    return emitted


SQL_DOCSTRINGS: dict[str, str] = {
    "rows": "Row projections shared by the repositories that read the same tables.",
    "getters": "Projections more than one repository needs, each taking a connection.",
    "sessions": "Conversation listing SQL: query shape, cursors and delete scoping.",
    "hosts": "Host column projections and the row mapper every host read returns.",
    "host_slots": "Host slot release, only ever called once an attempt is closed.",
    "sandboxes": "Sandbox grouping SQL shared by reserve, admission and settlement.",
    "ledger": "Inference-ledger capture and the window a refresh reads.",
    "session_forks": "Fork receipt SQL shared by prepare, finalize and recovery.",
    "events": "The one writer of job events, reused inside each caller's transaction.",
    "followups": "Follow-up sequencing, advanced or failed with its parent's state.",
    "attempts": "Attempt accounting helpers that two commands share.",
}

REPOSITORY_DOCSTRINGS: dict[str, str] = {
    "conversations": "Commands that create conversations and change their metadata.",
    "conversation_queries": "User-scoped conversation, job and project projections.",
    "attempts": "Claim, lease, cancellation and the atomic transition that ends a turn.",
    "execution_queries": "Observation reads over events, attempts and usage inputs.",
    "sandboxes": "Sandbox group activation, admission, settlement and reconciliation.",
    "residencies": "Residency lifecycles that outlive a single attempt.",
    "session_forks": "Fork preparation, materialization leases, receipts and recovery.",
    "snapshots": "Snapshot capture, publication and base import.",
    "host_pool": "Enrollment, heartbeat, capacity, placement policy and host reads.",
    "source_control": "Repository grants, OAuth state and GitLab connections.",
    "mcp": "Per-user MCP connection storage.",
    "grants": "Attempt and residency grant records, links and revocation queries.",
    "credential_queries": "The live credential fence the MCP relay resolves per call.",
    "ledger": "Usage snapshot reads, refresh queueing, leases and commits.",
    "audit": "Durable admin audit records.",
}


def main(argv: list[str] | None = None) -> int:
    """Report the split, or write it when ``--write`` is passed."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="emit the new modules")
    args = parser.parse_args(argv)

    source, tree, cls = load_store()
    public, private = inventory(source, cls)

    missing = sorted(set(APPENDIX) - set(public))
    extra = sorted(set(public) - set(APPENDIX) - {"initialize"})
    unmapped = sorted(set(private) - set(HELPER_MOVES))
    print(f"store public methods: {len(public)} (+ initialize)")
    print(f"appendix targets:     {len(APPENDIX)}")
    for label, problems in (
        ("MISSING from store", missing),
        ("NOT IN appendix", extra),
        ("private helpers with no home", unmapped),
    ):
        if problems:
            print(f"  {label}: {problems}")

    by_module: dict[str, list[str]] = {}
    for name, target in APPENDIX.items():
        by_module.setdefault(target.module, []).append(name)
    print(f"\nrepositories to emit: {len(by_module)}")
    for module in sorted(by_module):
        print(f"  {module + '.py':26s} {len(by_module[module]):3d} methods")

    if missing or extra or unmapped:
        return 1

    report = build_report(source, cls, public, private)
    print()
    print(report)
    if not args.write:
        return 0

    write_report(report)
    print(f"\nwrote {REQUALIFIED.relative_to(REPO)}")

    emitted = emit_domain(source, tree)
    emitted.update(emit_sql(source, tree, public, private))
    emitted.update(emit_repositories(source, public, private))
    for path, text in sorted(emitted.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
