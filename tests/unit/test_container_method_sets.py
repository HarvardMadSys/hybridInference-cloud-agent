"""Container classes split into mixins expose exactly the methods they had as one class.

``ChatSandboxManager`` was one class in ``container/chat_sandbox.py`` before its
exec, workspace, observer, marker and ``docker create`` methods moved into
mixins beside it; ``ChatSandboxNetwork`` lost its in-guest check to a mixin the
same way. The expected names are the methods (and properties) each class
resolved at commit ``a4b4ef436``, private ones included: a method lost in the
split, or a helper that leaked into the class, changes the set.
"""

from __future__ import annotations

from types import FunctionType

from cloud_agent_host.container.chat_sandbox import ChatSandboxManager
from cloud_agent_host.network.chat_sandbox_network import ChatSandboxNetwork


def _methods(cls: type) -> set[str]:
    """Every method and property the class and its bases define, as the class resolves them."""
    return {
        name
        for klass in cls.__mro__
        if klass is not object
        for name, value in vars(klass).items()
        if isinstance(value, FunctionType | staticmethod | classmethod | property)
    }


def test_the_chat_sandbox_manager_keeps_its_methods() -> None:
    assert _methods(ChatSandboxManager) == {
        "__init__",
        "_belongs_to_namespace",
        "_check_observer_self_test",
        "_clear_private_member_homes",
        "_encode_member_processes",
        "_guest_observer_controller",
        "_guest_observer_snapshot",
        "_kata_privileged_exec",
        "_member_reaper_script",
        "_observer_privileged_exec",
        "_parse_member_processes",
        "_probe_guest_observer_once",
        "_probe_member_isolation",
        "_prove_started_guest",
        "_remove_abandoned_probes",
        "_remove_created",
        "_validate_container_path",
        "_validate_extra_args",
        "_validate_member_identity",
        "_validate_member_pidfile",
        "begin_guest_observation",
        "build_create_command",
        "build_exec_command",
        "capture_member_processes",
        "clear_final_archive_marker",
        "create",
        "delete_initial_archive_marker",
        "ensure_codex_mcp_policy",
        "ensure_downloaded_runtime",
        "ensure_kilo_mcp_runtime",
        "ensure_mcp_helpers",
        "ensure_member_process_reaper",
        "ensure_private_member_collections",
        "exec_capture",
        "exec_client_env",
        "export_archive",
        "export_stopped_archive",
        "finish_guest_observation",
        "freeze_shared_writers",
        "guest_boot_id",
        "import_archive",
        "import_base_bundle",
        "inspect",
        "install_tool_hooks",
        "kill_member_process",
        "list",
        "member_window_journal_delta",
        "member_window_request",
        "network",
        "normalize_member_files",
        "operation_writer_uids",
        "pipe_member_exec",
        "prepare_shared_workspace",
        "probe_guest_observer",
        "read_environment_setup_marker",
        "read_final_archive_marker",
        "read_initial_archive_marker",
        "read_running_final_archive_marker",
        "read_stopped_final_archive_marker_optional",
        "reap_member_processes",
        "reap_member_processes_except",
        "reap_stale_terminal_processes",
        "release_stopped_networks",
        "remove_checkpoint_image",
        "remove_environment_test",
        "remove_stopped_archived",
        "remove_superseded",
        "require_docker_data_root",
        "require_existing",
        "require_network",
        "require_stopped_archive",
        "reset_unsettled_state",
        "spawn",
        "spawn_terminal",
        "start",
        "stop",
        "sync_guest_network",
        "thaw_shared_writers",
        "tool_mark_reader",
        "warm_guest_observer",
        "write_environment_setup_marker",
        "write_final_archive_marker",
        "write_initial_archive_marker",
        "write_member_inference_env",
        "write_stopped_final_archive_marker",
    }


def test_the_chat_sandbox_network_keeps_its_methods() -> None:
    assert _methods(ChatSandboxNetwork) == {
        "__init__",
        "_attach_relays",
        "_attach_to_group_network",
        "_candidate_chat_subnets",
        "_container_network_aliases",
        "_create_group_network",
        "_disconnect_endpoint",
        "_discover_relays",
        "_drop_relay_exceptions",
        "_endpoint_aliases",
        "_endpoint_is_current",
        "_ensure_group_network",
        "_ensure_group_services",
        "_ensure_service_connection",
        "_environment_run_attached",
        "_inspect_containers",
        "_inspect_network",
        "_is_subnet_allocation_conflict",
        "_network",
        "_network_aliases_for",
        "_network_is_stale_for_prune",
        "_network_labels",
        "_network_uses_chat_pool",
        "_owned_network_matches",
        "_reconcile_group_members",
        "_refuse_foreign_networks",
        "_relay_addresses_from",
        "_relay_aliases",
        "_relay_endpoint",
        "_relay_endpoint_from",
        "_relay_owners",
        "_release_orphan_network",
        "_release_owned_network",
        "_release_sandbox",
        "_release_sandbox_networks",
        "_require_group_network",
        "_restore_exceptions",
        "_sync_kata_guest_network",
        "_sync_relay_exceptions",
        "_verify_group_network",
        "_verify_group_snapshot",
        "_verify_guest_network",
        "backend",
        "network_for",
        "prune_orphan_networks",
        "relay_addresses",
        "renew_host_rules",
        "require_host_rules",
        "sync_ssh_targets",
        "verify_host_rules",
    }
