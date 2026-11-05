"""A repository split into mixins exposes exactly the methods it had as one class.

Each class below was one module before it became a package of mixins, one per
concern. The expected names are the methods its class body defined then, read
from that module at commit ``3e0400ef7``, private ones included: a method lost
in the split, or a helper that leaked into the class, changes the set.
"""

from __future__ import annotations

from types import FunctionType

from cloud_agent.storage.repositories.attempt_commands import PostgresAttemptCommands
from cloud_agent.storage.repositories.environments import PostgresEnvironmentRepository
from cloud_agent.storage.repositories.host_pool import PostgresHostPoolRepository
from cloud_agent.storage.repositories.mcp import PostgresMcpConnectionRepository


def _methods(cls: type) -> set[str]:
    """Every method the class and its mixins define, as the class resolves them."""
    return {
        name
        for klass in cls.__mro__
        if klass is not object
        for name, value in vars(klass).items()
        if isinstance(value, FunctionType | staticmethod | classmethod)
    }


def test_the_host_pool_repository_keeps_its_methods() -> None:
    assert _methods(PostgresHostPoolRepository) == {
        "__init__",
        "_host_row_to_dict",
        "_keep_capacity_history_isolated",
        "_record_host_capacity",
        "_record_host_capacity_isolated",
        "acquire_host_drain_fence",
        "activate_ready_runtime_upgrades",
        "active_runner_host",
        "authenticate_host",
        "autoscaling_observation",
        "consume_enroll_token",
        "deployment_runner_pool",
        "finish_host_removals",
        "forget_runner_host",
        "get_host",
        "host_for_attempt",
        "host_for_job",
        "host_settings",
        "issue_enroll_token",
        "list_runner_hosts",
        "queue_health",
        "reap_offline_hosts",
        "record_host_heartbeat",
        "register_host",
        "release_host_drain_fence",
        "remove_host",
        "request_runtime_upgrade",
        "runner_pool",
        "runtime_download_targets",
        "runtime_upgrade_snapshot",
        "sandbox_archive_settings",
        "set_active_runner_host",
        "set_host_settings",
        "set_host_slots_desired",
        "set_host_status",
        "set_runner_host_settings",
        "set_runtime_version",
        "set_sandbox_archive_settings",
    }


def test_the_attempt_commands_keep_their_methods() -> None:
    assert _methods(PostgresAttemptCommands) == {
        "__init__",
        "_append_events",
        "_attempts_spent",
        "_lock_live_attempt",
        "_reap_attempt",
        "_transition_job",
        "_turn_summary_decision",
        "append_event",
        "append_events",
        "claim_job",
        "heartbeat",
        "reap_expired",
        "record_attempt_resources",
        "record_attempt_turn_summary",
        "record_continuity_mode",
        "release_claim",
        "request_cancel",
        "sweep_queued_follow_ups",
        "transition",
    }


def test_the_mcp_connection_repository_keeps_its_methods() -> None:
    assert _methods(PostgresMcpConnectionRepository) == {
        "__init__",
        "_apply_mcp_allow_group",
        "_flush_remembered_approvals",
        "_insert_mcp_connection",
        "_lock_connection",
        "_lock_invocation",
        "_lock_oauth_flow",
        "_mcp_connection",
        "apply_pending_mcp_allow_rules",
        "claim_mcp_invocation",
        "claim_mcp_refresh",
        "close_mcp_transport_session",
        "consume_mcp_oauth_flow",
        "create_mcp_connection",
        "create_mcp_connections",
        "create_mcp_invocation",
        "create_mcp_oauth_flow",
        "decide_mcp_invocation",
        "disconnect_mcp_connection",
        "expire_mcp_invocation",
        "expire_mcp_invocations",
        "find_approved_mcp_invocation",
        "finish_mcp_invocation",
        "get_mcp_capabilities",
        "get_mcp_connection",
        "get_mcp_invocation",
        "list_mcp_bindings",
        "list_mcp_connections",
        "list_mcp_connectors",
        "list_mcp_invocations",
        "open_mcp_transport_session",
        "reap_mcp_transport_sessions",
        "release_mcp_refresh_claim",
        "replace_mcp_bindings",
        "save_mcp_capabilities",
        "set_mcp_client_registration",
        "set_mcp_connection_enabled",
        "set_mcp_connection_status",
        "set_mcp_credential",
        "update_mcp_connection",
    }


def test_the_environment_repository_keeps_its_methods() -> None:
    assert _methods(PostgresEnvironmentRepository) == {
        "__init__",
        "_environment_check_merged",
        "_environment_decode",
        "_environment_encode",
        "_environment_installations",
        "_environment_lock",
        "_environment_public",
        "_environment_publish",
        "_environment_redactor",
        "_environment_row",
        "_environment_run_view",
        "_environment_stored_variables",
        "_environment_version",
        "_require_team_binding_access",
        "_team_access",
        "_team_environment_audit",
        "_write_preparation",
        "archive_environment",
        "bind_current_environment",
        "cancel_environment_run",
        "claim_environment_run",
        "copy_environment",
        "create_environment_action_run",
        "create_environment_run",
        "current_setup_fingerprints",
        "environment_for_claim",
        "fail_environment_preflight",
        "find_environment_run",
        "get_environment",
        "get_environment_action_run",
        "get_environment_binding",
        "get_environment_run",
        "get_global_environment",
        "get_job_environment",
        "list_environment_orphans",
        "list_environment_versions",
        "list_environments",
        "maintain_environments",
        "record_environment_preparation",
        "record_worker_environment_preparation",
        "redact_environment_steps",
        "refresh_runtime_environment",
        "request_skip_environment_start",
        "restore_environment",
        "save_environment",
        "set_environment_default",
        "update_environment_action_run",
        "update_environment_run",
        "workspace_environment_view",
    }
