"""The admin audit trail's declarations: every written action, and what it reads as.

An action the trail records without a declaration still gets a row, filed as
``unknown`` — so nothing at runtime notices a writer that forgot to declare
its action. This file does: it reads every call that writes an audit row and
checks the action it names is in :data:`cloud_agent.domain.audit.ACTIONS`,
that the migration's frozen copy of the table matches it, and that the Audit
log page has the list it renders sentences for.
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from cloud_agent.domain.accounts import ADMIN_ACTIONS
from cloud_agent.domain.audit import (
    ACTIONS,
    AUDIT_CATEGORIES,
    actions_in,
    actor_account_id,
    attribute,
)

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "apps" / "backend" / "cloud_agent"
MIGRATION = (
    ROOT / "apps" / "backend" / "migrations" / "versions" / "0114_admin_audit_attribution.py"
)
PAGE_ACTIONS = (
    ROOT / "apps" / "frontend" / "src" / "app" / "(agents)" / "admin" / "audit" / "actions.json"
)

#: The calls that write a row, and where the action is when passed positionally.
_WRITERS = {
    "log_admin_action": 2,
    "write_audit": None,
    "record_account_audit": None,
    "record_admin_action": None,
    "insert_audit_row": None,
    "_audit_admin_deletion": None,
    "_set_status": None,
}

#: Writes whose action is computed. Each is checked against what it expands to
#: below, so a new computed name is a deliberate addition here.
_COMPUTED = {
    ("api/admin_compute.py", "log_admin_action"),  # f"agent.compute.{action}", see _compute_actions
    ("api/admin_hosts.py", "log_admin_action"),  # _set_status(action=...), literal at its callers
    ("storage/repositories/accounts.py", "write_audit"),  # f"agent.local_account.{action}"
    (
        "storage/repositories/sandbox/admin_deletion.py",
        "insert_audit_row",
    ),  # _audit_admin_deletion(action=...)
    ("storage/_sql/local_accounts.py", "insert_audit_row"),  # write_audit(action=...)
    ("storage/repositories/account_policy.py", "write_audit"),  # record_account_audit(action=...)
    ("storage/repositories/audit.py", "insert_audit_row"),  # record_admin_action(action=...)
    ("api/deps.py", "record_admin_action"),  # log_admin_action(action=...)
    # _audit(request, store, actor, "<name>", ...), see _provided_model_actions
    ("api/admin_operator_models.py", "log_admin_action"),
}


def _name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return func.id if isinstance(func, ast.Name) else None


def _action_argument(call: ast.Call, position: int | None) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "action":
            return keyword.value
    if position is not None and len(call.args) > position:
        return call.args[position]
    return None


def _literals(node: ast.expr) -> list[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        body, orelse = _literals(node.body), _literals(node.orelse)
        return body + orelse if body is not None and orelse is not None else None
    return None


def _writes() -> tuple[set[str], set[tuple[str, str]]]:
    literal: set[str] = set()
    computed: set[tuple[str, str]] = set()
    for path in BACKEND.rglob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or (name := _name(node)) not in _WRITERS:
                continue
            argument = _action_argument(node, _WRITERS[name])
            if argument is None:
                continue
            names = _literals(argument)
            if names is None:
                computed.add((str(path.relative_to(BACKEND)), name))
            else:
                literal.update(names)
    return literal, computed


def _compute_actions() -> set[str]:
    """The names ``admin_compute._audit(request, actor, "<name>", ...)`` writes."""
    tree = ast.parse((BACKEND / "api" / "admin_compute.py").read_text())
    return {
        f"agent.compute.{node.args[2].value}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _name(node) == "_audit"
        and len(node.args) > 2
        and isinstance(node.args[2], ast.Constant)
    }


def _provided_model_actions() -> set[str]:
    """The names ``admin_operator_models._audit(request, store, actor, "<name>", ...)`` writes."""
    tree = ast.parse((BACKEND / "api" / "admin_operator_models.py").read_text())
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _name(node) == "_audit" and len(node.args) > 3:
            literals = _literals(node.args[3])
            assert literals is not None, "an audit action there must be spelled out"
            names.update(literals)
    return names


def test_every_literal_action_a_writer_names_is_declared() -> None:
    literal, _ = _writes()
    assert literal, "the scan found no audit writes; the writer names above have drifted"
    assert sorted(literal - set(ACTIONS)) == []


def test_every_computed_action_is_known_and_declared() -> None:
    _, computed = _writes()
    assert computed == _COMPUTED
    compute = _compute_actions()
    assert compute, "admin_compute writes no audit row any more?"
    assert sorted(compute - set(ACTIONS)) == []
    accounts = {f"agent.local_account.{name}" for name in ADMIN_ACTIONS}
    assert sorted(accounts - set(ACTIONS)) == []
    provided = _provided_model_actions()
    assert len(provided) == 9, "admin_operator_models writes a different set of actions?"
    assert sorted(provided - set(ACTIONS)) == []


def test_reads_and_checks_are_not_audited() -> None:
    """Only changes and access to someone's data are recorded."""
    assert "agent.compute.connection.validate" not in _compute_actions()
    assert not [
        name
        for name in ACTIONS
        if name.endswith((".list", ".read")) and name != "agent.run.event_content.read"
    ]


def _frozen_table() -> tuple[tuple[str, str, str | None, str | None, str | None, bool], ...]:
    spec = importlib.util.spec_from_file_location("audit_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._FROZEN


#: Declared after migration 0114 shipped. No row of theirs predates it, so its
#: frozen copy has nothing to backfill for them and does not list them.
_DECLARED_AFTER_0114 = {
    "agent.local_account.import",
    "agent.operator_model_connection.create",
    "agent.operator_model_connection.update",
    "agent.operator_model_connection.rotate_key",
    "agent.operator_model_connection.enable",
    "agent.operator_model_connection.disable",
    "agent.operator_model_connection.delete",
    "agent.operator_model_connection.set_limit",
    "agent.operator_model_user_limit.set",
    "agent.operator_model_user_limit.clear",
    "agent.local_account.provision_invite",
    "agent.local_account.provision_revoke",
}


def test_the_migration_backfills_exactly_what_the_registry_declares() -> None:
    """The migration's copy is frozen: what the registry declared when it shipped."""
    declared = {
        (
            action.name,
            action.target_type,
            action.target_key,
            action.fixed_target,
            action.correlation_key,
            action.asynchronous,
        )
        for action in ACTIONS.values()
        if action.name not in _DECLARED_AFTER_0114
    }
    assert set(_frozen_table()) == declared
    assert ACTIONS.keys() >= _DECLARED_AFTER_0114


def _page_list() -> list[dict[str, str]]:
    return [
        {"action": action.name, "category": action.category, "kind": action.kind}
        for action in ACTIONS.values()
    ]


def test_the_audit_page_knows_every_declared_action() -> None:
    """The page's sentences are checked against this list by its own tests.

    Regenerate after changing the registry:
    ``uv run python -c "from tests.unit.test_audit_registry import write_page_list; write_page_list()"``
    """
    assert json.loads(PAGE_ACTIONS.read_text()) == _page_list()


def write_page_list() -> None:
    """Rewrite the page's list from the registry."""
    PAGE_ACTIONS.write_text(json.dumps(_page_list(), indent=2) + "\n")


def test_every_category_shows_something() -> None:
    for category in AUDIT_CATEGORIES:
        assert actions_in(category), category


@pytest.mark.parametrize(
    ("actor", "account"),
    [
        ("admin:01KQ", "01KQ"),
        ("self:01M6", "01M6"),
        ("admin-token", None),
        ("admin:", None),
        ("sandbox-deletion-worker", None),
        (None, None),
    ],
)
def test_an_actor_names_an_account_only_when_it_is_one(
    actor: str | None, account: str | None
) -> None:
    assert actor_account_id(actor) == account


def test_a_row_reads_as_what_it_acted_on() -> None:
    drain = attribute(
        "agent.runner_host.drain", details={"host": "h1"}, subject_user_id=None, success=True
    )
    assert (drain.target_type, drain.target_id, drain.outcome) == ("host", "h1", "applied")

    approve = attribute(
        "agent.local_account.approve", details={}, subject_user_id="acct", success=True
    )
    assert (approve.target_type, approve.target_id) == ("account", "acct")

    delete = attribute(
        "agent.sandbox.delete.requested",
        details={"sandbox_id": "asbx", "request_id": "req"},
        subject_user_id="owner",
        success=True,
    )
    assert (delete.target_type, delete.target_id) == ("sandbox", "asbx")
    assert (delete.correlation_id, delete.outcome) == ("req", "requested")

    retire = attribute(
        "agent.compute.allocation.retire",
        details={"allocation_id": "al", "operation_id": "op"},
        subject_user_id=None,
        success=True,
    )
    assert (retire.target_type, retire.target_id, retire.correlation_id) == (
        "compute_server",
        "al",
        "op",
    )

    settings = attribute("agent.settings.update", details=None, subject_user_id=None, success=True)
    assert (settings.target_type, settings.target_id) == ("setting", "application")


def test_a_recorded_outcome_wins_over_success() -> None:
    refused = attribute(
        "agent.profiling.start", details={"outcome": "refused"}, subject_user_id=None, success=True
    )
    replayed = attribute(
        "agent.profiling.stop", details={"outcome": "replayed"}, subject_user_id=None, success=True
    )
    failed = attribute("agent.compute.server.add", details={}, subject_user_id=None, success=False)
    assert (refused.outcome, replayed.outcome, failed.outcome) == ("refused", "replayed", "failed")


def test_an_undeclared_action_still_gets_a_row() -> None:
    unknown = attribute("agent.something.new", details=None, subject_user_id="acct", success=True)
    assert (unknown.target_type, unknown.target_id, unknown.outcome) == (
        "unknown",
        "acct",
        "applied",
    )
