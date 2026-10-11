"""Automatic setup discovers one deployment and limits each generated SSH identity."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.hazmat.primitives import serialization
from pydantic import ValidationError

from cloud_agent.ports.external.compute_provider import ProviderError, SshConnection
from cloud_agent.providers.compute.setup_tunnels import (
    _REMOTE_SCRIPT,
    ControlPlaneEndpoint,
    SshTunnelSetup,
    generate_tunnel_key,
    tunnel_username,
)

SHA = "a" * 40
IDENTITY = "b" * 64
SSH = SshConnection(
    address="control.example.org",
    username="admin",
    credential_ref="management-key",
    known_hosts_ref="verified-control-host-key",
)
ENDPOINT = ControlPlaneEndpoint(
    main_port=8100, mcp_port=8101, gateway_ip="172.20.0.1", machine_identity=IDENTITY
)


def remote():
    """Load trusted script functions without executing its remote entry point."""
    namespace = {"__name__": "test_remote_script"}
    exec(compile(_REMOTE_SCRIPT, "setup_tunnels", "exec"), namespace)
    return namespace


def test_generated_keys_are_distinct_valid_and_redacted():
    first, second = generate_tunnel_key(), generate_tunnel_key()
    assert first.public_key != second.public_key
    assert first.private_key.get_secret_value() not in repr(first)
    loaded = serialization.load_ssh_private_key(first.private_key.get_secret_value().encode(), None)
    assert (
        loaded.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode()
        == first.public_key
    )


def test_tunnel_accounts_are_stable_distinct_and_never_management_user():
    names = {tunnel_username("setup-1", role) for role in ("inference", "mcp", "control")}
    assert len(names) == 3
    assert all(name.startswith("ca-") and len(name) < 32 and name != "admin" for name in names)
    assert tunnel_username("setup-1", "mcp") == tunnel_username("setup-1", "mcp")
    assert tunnel_username("setup-2", "mcp") not in names
    for setup_id, role in [("bad\nMatch all", "mcp"), ("okay", "shell")]:
        with pytest.raises(ProviderError):
            tunnel_username(setup_id, role)


@pytest.mark.parametrize(
    "address", ["0.0.0.0", "127.0.0.1", "169.254.1.2", "8.8.8.8", "::1", "192.0.2.1", "240.0.0.1"]
)
def test_endpoint_rejects_non_bridge_addresses(address):
    with pytest.raises(ValidationError):
        ControlPlaneEndpoint.model_validate({**ENDPOINT.model_dump(), "gateway_ip": address})


@pytest.mark.asyncio
async def test_transport_uses_fixed_script_and_json_stdin_only():
    transport = AsyncMock()
    transport.run.return_value = json.dumps({"ok": True})
    helper = SshTunnelSetup(transport=transport)
    key = generate_tunnel_key()
    await helper.provision_gateway(SSH, key.public_key, 8080, "setup-1")
    call = transport.run.call_args
    assert call.args[0] == SSH
    assert call.args[1].startswith("sudo -n python3 -c ")
    assert key.public_key not in call.args[1]
    params = json.loads(call.kwargs["stdin"])
    assert params["key"] == key.public_key
    assert params["inference_username"] == tunnel_username("setup-1", "inference")
    assert key.private_key.get_secret_value() not in call.kwargs["stdin"].decode()


@pytest.mark.asyncio
async def test_discovery_parses_endpoint_and_failures_do_not_echo_remote_material():
    transport = AsyncMock()
    helper = SshTunnelSetup(transport=transport)
    transport.run.return_value = json.dumps({"ok": True, "endpoint": ENDPOINT.model_dump()})
    assert await helper.discover_control_plane(SSH, SHA) == ENDPOINT
    transport.run.return_value = json.dumps({"ok": False, "reason": "control_plane_ambiguous"})
    with pytest.raises(ProviderError, match="Several control planes") as exc:
        await helper.discover_control_plane(SSH, SHA)
    assert exc.value.code == "control_plane_ambiguous"
    transport.run.return_value = "PRIVATE KEY SECRET ENV"
    with pytest.raises(ProviderError) as exc:
        await helper.discover_control_plane(SSH, SHA)
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.asyncio
async def test_gateway_check_returns_verified_machine_identity():
    transport = AsyncMock()
    transport.run.return_value = json.dumps({"ok": True, "machine_identity": IDENTITY})
    helper = SshTunnelSetup(transport=transport)
    assert await helper.check_gateway(SSH, 8080) == IDENTITY
    for identity in [None, "raw-machine-id", 123, "x" * 64]:
        transport.run.return_value = json.dumps({"ok": True, "machine_identity": identity})
        with pytest.raises(ProviderError, match="invalid machine identity"):
            await helper.check_gateway(SSH, 8080)
    transport.run.return_value = json.dumps({"ok": False, "reason": "PRIVATE KEY SECRET ENV"})
    with pytest.raises(ProviderError) as exc:
        await helper.discover_control_plane(SSH, SHA)
    assert "PRIVATE" not in str(exc.value)


@pytest.mark.asyncio
async def test_control_requires_distinct_keys_ports_and_safe_identifiers():
    transport = AsyncMock()
    helper = SshTunnelSetup(transport=transport)
    key = generate_tunnel_key().public_key
    for other, reverse, setup_id in [
        (key, 8092, "setup-1"),
        (generate_tunnel_key().public_key, 8100, "setup-1"),
        (generate_tunnel_key().public_key, 8092, "bad;useradd root"),
        (key + "\nother-key", 8092, "setup-1"),
    ]:
        with pytest.raises(ProviderError):
            await helper.provision_control(SSH, key, other, ENDPOINT, reverse, setup_id)
    transport.run.assert_not_awaited()
    with pytest.raises(ProviderError):
        await helper.check_gateway(SSH, True)
    with pytest.raises(ProviderError):
        await helper.discover_control_plane(SSH, "HEAD")


def inventory(namespace, tmp_path, *, ambiguous=False, same_image=True, gateway="172.20.0.1"):
    containers = {
        "main": {
            "id": "main",
            "image": "control-image",
            "labels": {
                "com.docker.compose.project": "release-project",
                "io.hybridinference.deployment.sha": SHA,
            },
            "ports": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8100"}]},
            "networks": {"control-net": {}, "other-net": {}},
        },
        "relay": {
            "id": "relay",
            "image": "control-image" if same_image else "foreign-image",
            "labels": {},
            "ports": {"8001/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8101"}]},
            "networks": {"control-net": {}},
        },
    }
    commands = []

    def command(args):
        commands.append(args)
        if args[:3] == ["docker", "container", "ls"]:
            if "label=com.docker.compose.service=control-plane" in args:
                return "main main" if ambiguous else "main"
            assert "label=com.docker.compose.project=release-project" in args
            return "relay"
        if args[:3] == ["docker", "container", "inspect"]:
            assert ".Config.Env" not in args[4]
            return json.dumps(containers[args[-1]])
        if args[:3] == ["docker", "network", "inspect"]:
            return json.dumps(
                {
                    "Driver": "bridge",
                    "Labels": {
                        "com.docker.compose.project": "release-project",
                        "com.docker.compose.network": "control-plane",
                    },
                    "IPAM": {"Config": [{"Gateway": gateway, "Subnet": "172.20.0.0/16"}]},
                }
            )
        raise AssertionError(args)

    identity_path = tmp_path / "machine-id"
    identity_path.write_text("f" * 32 + "\n")
    namespace["command"] = command
    namespace["Path"] = lambda _: identity_path
    namespace["listening"] = lambda port: None
    return commands, containers


def test_remote_discovery_selects_exact_release_project_and_reads_no_environment(tmp_path):
    namespace = remote()
    commands, containers = inventory(namespace, tmp_path)
    result = namespace["discover"]({"deployment_sha": SHA})
    assert result["gateway_ip"] == "172.20.0.1"
    assert result["main_port"] == 8100 and result["mcp_port"] == 8101
    assert len(result["machine_identity"]) == 64
    assert all(".Config.Env" not in " ".join(args) for args in commands)
    containers["main"]["labels"]["io.hybridinference.deployment.sha"] = "c" * 40
    with pytest.raises(namespace["Blocked"], match="control_plane_missing"):
        namespace["discover"]({"deployment_sha": SHA})


@pytest.mark.parametrize(
    "updates,reason",
    [
        ({"ambiguous": True}, "control_plane_ambiguous"),
        ({"same_image": False}, "control_plane_missing"),
        ({"gateway": "0.0.0.0"}, "control_plane_network"),
    ],
)
def test_remote_discovery_refuses_ambiguous_release_or_wrong_listener(tmp_path, updates, reason):
    namespace = remote()
    inventory(namespace, tmp_path, **updates)
    with pytest.raises(namespace["Blocked"], match=reason):
        namespace["discover"]({"deployment_sha": SHA})


def test_remote_role_policies_and_key_options_enforce_separate_audiences():
    namespace = remote()
    local, text = namespace["desired_policy"]("ca-i-test", 8080)
    assert local["allowtcpforwarding"] == "local"
    assert local["permitlisten"] == "none"
    assert local["permitopen"] == "127.0.0.1:8080"
    assert local["allowstreamlocalforwarding"] == "no"
    assert local["forcecommand"] == "/usr/sbin/nologin"
    assert local["permittunnel"] == "no"
    assert "Match User ca-i-test\n" in text and text.endswith("Match all\n")
    control, _ = namespace["desired_policy"]("ca-c-test", 8100, "172.20.0.1:18092")
    assert control["permitopen"] == "127.0.0.1:8100"
    assert control["permitlisten"] == "172.20.0.1:18092"
    assert control["gatewayports"] == "clientspecified"
    keys = namespace["entries"](
        {
            "action": "control",
            "setup_id": "slot-1",
            "mcp_key": "ssh-ed25519 MCP",
            "control_key": "ssh-ed25519 CONTROL",
            "endpoint": ENDPOINT.model_dump(),
            "reverse_port": 18092,
        }
    )
    assert 'command="",restrict,port-forwarding,permitopen="127.0.0.1:8101"' in keys[0]
    assert "8100" not in keys[0] and "permitlisten" not in keys[0]
    assert 'permitlisten="172.20.0.1:18092"' in keys[1]
    assert "8101" not in keys[1]


def test_remote_provision_is_atomic_idempotent_and_refuses_key_conflicts(tmp_path):
    namespace = remote()
    path = tmp_path / "authorized_keys"
    user = SimpleNamespace(pw_uid=1000, pw_gid=1000)
    namespace["key_path"] = lambda username: (path, user)
    namespace["ensure_policy"] = lambda *args: None
    writes = []

    def write(target, content, uid, gid, mode):
        assert target == path and mode == 0o600 and uid == 1000 and gid == 1000
        writes.append(content)
        target.write_text(content)

    namespace["atomic_write"] = write
    namespace["provision_account"]("ca-m-test", "restricted public-key marker", 8101)
    namespace["provision_account"]("ca-m-test", "restricted public-key marker", 8101)
    assert writes == ["restricted public-key marker\n"]
    with pytest.raises(namespace["Blocked"], match="setup_key_conflict"):
        namespace["provision_account"]("ca-m-test", "different public-key marker", 8101)


def test_reverse_port_must_be_free_but_saved_setup_retry_preserves_live_tunnel(tmp_path):
    namespace = remote()
    path = tmp_path / "authorized_keys"
    namespace["key_path"] = lambda username: (path, SimpleNamespace(pw_uid=1000, pw_gid=1000))
    namespace["ensure_policy"] = lambda *args: None

    class BusySocket:
        def bind(self, address):
            raise OSError("address in use")

        def close(self):
            pass

    namespace["socket"] = SimpleNamespace(
        AF_INET=2, SOCK_STREAM=1, socket=lambda *args: BusySocket()
    )
    with pytest.raises(namespace["Blocked"], match="reverse_port_unavailable"):
        namespace["provision_account"]("ca-c-test", "saved entry", 8100, "172.20.0.1:18092")
    assert not path.exists()
    path.write_text("saved entry\n")
    namespace["provision_account"]("ca-c-test", "saved entry", 8100, "172.20.0.1:18092")


def test_policy_validation_rolls_back_fragment_before_authorizing_keys(tmp_path):
    namespace = remote()
    directory = tmp_path / "sshd_config.d"
    directory.mkdir()
    namespace["Path"] = lambda _: directory
    namespace["ssh_policy"] = lambda _: {}
    namespace["user_groups"] = lambda _: {"tunnel"}
    namespace["check_conditional_policy"] = lambda *args: None
    namespace["atomic_write"] = lambda path, content, *args: path.write_text(content)
    namespace["command"] = lambda args: ""
    with pytest.raises(namespace["Blocked"], match="ssh_policy_conflict"):
        namespace["ensure_policy"]("ca-c-test", 8100, "172.20.0.1:18092")
    assert list(directory.iterdir()) == []


def test_policy_failure_restores_existing_fragment_and_never_reloads(tmp_path):
    namespace = remote()
    directory = tmp_path / "sshd_config.d"
    directory.mkdir()
    fragment = directory / "00-cloud-agent-tunnels-ca-c-test.conf"
    fragment.write_text("original policy\n")
    namespace["Path"] = lambda _: directory
    namespace["ssh_policy"] = lambda _: {}
    namespace["user_groups"] = lambda _: {"tunnel"}
    namespace["check_conditional_policy"] = lambda *args: None
    namespace["atomic_write"] = lambda path, content, *args: path.write_text(content)
    commands = []

    def command(args):
        commands.append(args)
        namespace["refuse"]("setup_failed")

    namespace["command"] = command
    with pytest.raises(namespace["Blocked"], match="setup_failed"):
        namespace["ensure_policy"]("ca-c-test", 8100, "172.20.0.1:18092")
    assert fragment.read_text() == "original policy\n"
    assert commands == [["/usr/sbin/sshd", "-t"]]


def test_completed_policy_retry_validates_and_reloads_without_rewriting_files(tmp_path):
    namespace = remote()
    directory = tmp_path / "sshd_config.d"
    directory.mkdir()
    namespace["Path"] = lambda _: directory
    desired, content = namespace["desired_policy"]("ca-m-test", 8101)
    policies = iter([{}, desired, desired, desired])
    namespace["ssh_policy"] = lambda _: next(policies)
    namespace["user_groups"] = lambda _: {"tunnel"}
    namespace["check_conditional_policy"] = lambda *args: None
    writes, validations = [], []

    def write(path, content, *args):
        writes.append(path)
        path.write_text(content)

    namespace["atomic_write"] = write
    namespace["command"] = lambda args: validations.append(args) or ""
    reloads = []

    def reload(args, **kwargs):
        reloads.append(args)
        return SimpleNamespace(returncode=0)

    namespace["subprocess"] = SimpleNamespace(run=reload, DEVNULL=-3)
    namespace["ensure_policy"]("ca-m-test", 8101)
    assert reloads == [["systemctl", "reload", "ssh"]]
    assert (directory / "00-cloud-agent-tunnels-ca-m-test.conf").read_text() == content
    namespace["ensure_policy"]("ca-m-test", 8101)
    assert len(reloads) == 2
    assert writes == [directory / "00-cloud-agent-tunnels-ca-m-test.conf"]
    assert validations == [["/usr/sbin/sshd", "-t"], ["/usr/sbin/sshd", "-t"]]


def test_interrupted_policy_write_requires_successful_reload_before_authorizing_key(tmp_path):
    namespace = remote()
    directory = tmp_path / "sshd_config.d"
    directory.mkdir()
    desired, content = namespace["desired_policy"]("ca-m-test", 8101)
    fragment = directory / "00-cloud-agent-tunnels-ca-m-test.conf"
    fragment.write_text(content)
    keys = tmp_path / "authorized_keys"
    namespace["key_path"] = lambda _: (keys, SimpleNamespace(pw_uid=1000, pw_gid=1000))
    namespace["ssh_policy"] = lambda _: desired
    namespace["user_groups"] = lambda _: {"tunnel"}
    namespace["check_conditional_policy"] = lambda *args: None
    validations = []
    namespace["command"] = lambda args: validations.append(args) or ""
    daemon_restricted = False
    reload_succeeds = False

    def reload(args, **kwargs):
        nonlocal daemon_restricted
        assert not keys.exists()
        if reload_succeeds:
            daemon_restricted = True
        return SimpleNamespace(returncode=0 if reload_succeeds else 1)

    def authorize(path, content, *args):
        assert path == keys and daemon_restricted
        path.write_text(content)

    namespace["subprocess"] = SimpleNamespace(run=reload, DEVNULL=-3)
    namespace["atomic_write"] = authorize
    with pytest.raises(namespace["Blocked"], match="ssh_reload_failed"):
        namespace["provision_account"]("ca-m-test", "restricted saved key", 8101)
    assert not keys.exists()
    assert fragment.read_text() == content
    assert not daemon_restricted
    reload_succeeds = True
    namespace["provision_account"]("ca-m-test", "restricted saved key", 8101)
    assert daemon_restricted and keys.read_text() == "restricted saved key\n"
    assert fragment.read_text() == content
    assert validations == [["/usr/sbin/sshd", "-t"], ["/usr/sbin/sshd", "-t"]]


def test_actual_sshd_parser_restores_include_context_and_preserves_other_users(tmp_path):
    """Exercise the production fragments against OpenSSH without starting a daemon."""
    daemon = Path("/usr/sbin/sshd")
    if not daemon.is_file():
        pytest.skip("OpenSSH server parser is not installed")
    host_key = tmp_path / "host-key"
    host_key.write_text(generate_tunnel_key().private_key.get_secret_value())
    host_key.chmod(0o600)
    fragments = tmp_path / "sshd_config.d"
    fragments.mkdir()
    namespace = remote()
    expected = {}
    for username, port, reverse in [
        ("ca-i-test", 8080, None),
        ("ca-m-test", 8101, None),
        ("ca-c-test", 8100, "172.20.0.1:18092"),
    ]:
        policy, content = namespace["desired_policy"](username, port, reverse)
        expected[username] = policy
        (fragments / (username + ".conf")).write_text(content)
    config = tmp_path / "sshd_config"
    global_options = "UsePAM yes\nSubsystem sftp internal-sftp\n"
    config.write_text(f"HostKey {host_key}\nInclude {fragments}/*.conf\n" + global_options)
    baseline = tmp_path / "baseline_config"
    baseline.write_text(f"HostKey {host_key}\n" + global_options)

    def inspected(configuration, username, address="127.0.0.1"):
        result = subprocess.run(
            [
                str(daemon),
                "-T",
                "-f",
                str(configuration),
                "-C",
                "user=" + username + ",host=localhost,addr=" + address,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr
        return dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)

    # OpenSSH restores the containing file's Match state after each Include:
    # https://github.com/openssh/openssh-portable/blob/V_8_9_P1/servconf.c#L1980-L1999
    for username, policy in expected.items():
        actual = inspected(config, username)
        assert all(actual.get(key) == value for key, value in policy.items())
        assert actual["usepam"] == "yes"
    assert inspected(config, "admin") == inspected(baseline, "admin")
    assert inspected(config, "other-service") == inspected(baseline, "other-service")

    # Earlier matching restrictions retain precedence; ensure_policy's exact
    # comparison must reject them instead of authorizing keys under another policy.
    config.write_text(
        f"HostKey {host_key}\n"
        + global_options
        + "Match User ca-i-test\n    AllowTcpForwarding no\nMatch all\n"
        + f"Include {fragments}/*.conf\n"
    )
    assert inspected(config, "ca-i-test")["allowtcpforwarding"] == "no"
    config.write_text(
        f"HostKey {host_key}\n"
        + global_options
        + "Match User ca-i-test Address 192.0.2.*\n"
        + " AllowTcpForwarding yes\n ChallengeResponseAuthentication yes\nMatch all\n"
        + f"Include {fragments}/*.conf\n"
    )
    local = inspected(config, "ca-i-test")
    remote_address = inspected(config, "ca-i-test", "192.0.2.1")
    assert local["allowtcpforwarding"] == "local" and local["kbdinteractiveauthentication"] == "no"
    assert remote_address["allowtcpforwarding"] == "yes"
    assert remote_address["kbdinteractiveauthentication"] == "yes"
    config.write_text(
        f"HostKey {host_key}\n"
        + global_options
        + "AllowUsers ca-* admin\nDenyUsers blocked-* other\n"
    )
    namespace["command"] = lambda args: (
        subprocess.run(
            [
                str(daemon),
                "-T",
                "-f",
                str(config),
                "-C",
                "user=ca-m-test,host=localhost,addr=127.0.0.1",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout
    )
    policy = namespace["ssh_policy"]("ca-m-test")
    assert policy["allowusers"] == "ca-* admin"
    assert policy["denyusers"] == "blocked-* other"
    namespace["check_admission"]("ca-m-test", {"tunnel"}, policy)


@pytest.mark.parametrize(
    "policy",
    [
        {"allowusers": "admin"},
        {"allowusers": "ca-m-test@192.0.2.*"},
        {"allowusers": "ca-[mc]-test"},
        {"denyusers": "ca-*"},
        {"denyusers": "ca-m-test@192.0.2.*"},
        {"allowgroups": "admins"},
        {"denygroups": "tunnel"},
        {"pubkeyacceptedalgorithms": "rsa-sha2-256,rsa-sha2-512"},
    ],
)
def test_admission_refuses_existing_user_or_group_restrictions(policy):
    namespace = remote()
    with pytest.raises(namespace["Blocked"], match="ssh_admission_blocked"):
        namespace["check_admission"]("ca-m-test", {"tunnel"}, policy)


def test_admission_accepts_unconditional_accounts_and_exact_ssh_wildcard_semantics():
    namespace = remote()
    namespace["check_admission"](
        "ca-m-test",
        {"tunnel", "services"},
        {
            "allowusers": "admin ca-?-*",
            "denyusers": "blocked-* admin@192.0.2.*",
            "allowgroups": "admins services",
            "denygroups": "blocked-*",
        },
    )
    assert not namespace["pattern_matches"]("ca-m-test", "ca-[mc]-test")
    assert namespace["pattern_list_matches"]({"ca-m-test"}, "ca-*,!ca-i-*")
    assert not namespace["pattern_list_matches"]({"ca-m-test"}, "*,!ca-m-test")


def test_effective_policy_retains_every_admission_pattern():
    namespace = remote()
    namespace["command"] = lambda args: (
        "allowusers ca-*\nallowusers admin\nallowgroups tunnel\nallowgroups admins\n"
        "denyusers other\ndenyusers blocked-*\ndenygroups blocked\ndenygroups banned\n"
        "allowtcpforwarding local\n"
    )
    policy = namespace["ssh_policy"]("ca-m-test")
    assert policy["allowusers"] == "ca-* admin"
    assert policy["denyusers"] == "other blocked-*"
    assert policy["allowgroups"] == "tunnel admins"
    assert policy["denygroups"] == "blocked banned"
    namespace["check_admission"]("ca-m-test", {"tunnel"}, policy)


def test_admission_checks_all_nss_supplementary_groups():
    namespace = remote()
    namespace["pwd"] = SimpleNamespace(getpwnam=lambda _: SimpleNamespace(pw_gid=1000))
    namespace["os"] = SimpleNamespace(getgrouplist=lambda username, primary: [primary, 2000])
    namespace["grp"] = SimpleNamespace(
        getgrgid=lambda identity: SimpleNamespace(
            gr_name="tunnel" if identity == 1000 else "blocked-supplementary"
        )
    )
    groups = namespace["user_groups"]("ca-m-test")
    assert groups == {"tunnel", "blocked-supplementary"}
    with pytest.raises(namespace["Blocked"], match="ssh_admission_blocked"):
        namespace["check_admission"]("ca-m-test", groups, {"denygroups": "blocked-*"})


@pytest.mark.parametrize(
    "pattern,matched",
    [
        ("*,!blocked", False),
        ("!blocked,*", False),
        ("tunnel,!blocked", False),
        ("tunnel,!other", True),
        ("!other", False),
    ],
)
def test_conditional_match_group_negation_vetoes_all_other_memberships(tmp_path, pattern, matched):
    """Preserve OpenSSH's global negation veto across primary and NSS groups."""
    # ga_match_pattern_list returns 0 immediately on any membership's -1 result:
    # https://github.com/openssh/openssh-portable/blob/V_9_6_P1/groupaccess.c#L94-L110
    namespace = remote()
    groups = {"tunnel", "blocked"}
    assert namespace["match_context"](
        ["Group", pattern, "Address", "192.0.2.0/24"], "ca-m-test", groups
    ) == (matched, True)
    config = tmp_path / "sshd_config"
    config.write_text(f"Match Group {pattern} Address 192.0.2.0/24\n AllowTcpForwarding yes\n")
    namespace["Path"] = lambda path: config if path == "/etc/ssh/sshd_config" else Path(path)
    desired, _ = namespace["desired_policy"]("ca-m-test", 8101)
    if matched:
        with pytest.raises(namespace["Blocked"], match="ssh_conditional_policy"):
            namespace["check_conditional_policy"]("ca-m-test", groups, desired)
    else:
        namespace["check_conditional_policy"]("ca-m-test", groups, desired)


@pytest.mark.parametrize(
    "configuration,blocked",
    [
        ("Match Address 192.0.2.0/24\n AllowTcpForwarding yes\n", True),
        ("Match Address 192.0.2.0/24\n PermitListen 0.0.0.0:8092\n", True),
        ("Match Host *.example.org\n GatewayPorts yes\n", True),
        ("Match LocalAddress 192.0.2.1\n PermitTunnel yes\n", True),
        ("Match LocalPort 2222\n DenyUsers ca-*\n", True),
        ("Match Address 192.0.2.0/24\n ChallengeResponseAuthentication yes\n", True),
        ("Match Address 192.0.2.0/24\n PubkeyAcceptedKeyTypes -ssh-ed25519\n", True),
        ("Match User ca-* Address 192.0.2.0/24\n PermitOpen 127.0.0.1:8100\n", True),
        ("Match User admin Address 192.0.2.0/24\n AllowTcpForwarding yes\n", False),
        ("Match User *,!ca-m-test Address 192.0.2.0/24\n AllowTcpForwarding yes\n", False),
        ("Match Group admins Address 192.0.2.0/24\n AllowTcpForwarding yes\n", False),
        ("Match Group *,!tunnel Address 192.0.2.0/24\n AllowTcpForwarding yes\n", False),
        ("Match Address 192.0.2.0/24\n PermitOpen 127.0.0.1:8101\n", False),
        ("Match Address 192.0.2.0/24\n LogLevel DEBUG\n", False),
    ],
)
def test_conditional_policy_checks_future_addresses_and_skips_excluded_identities(
    tmp_path, configuration, blocked
):
    namespace = remote()
    config = tmp_path / "sshd_config"
    config.write_text(configuration)
    namespace["Path"] = lambda path: config if path == "/etc/ssh/sshd_config" else Path(path)
    desired, _ = namespace["desired_policy"]("ca-m-test", 8101)
    if blocked:
        with pytest.raises(namespace["Blocked"], match="ssh_conditional_policy"):
            namespace["check_conditional_policy"]("ca-m-test", {"tunnel"}, desired)
    else:
        namespace["check_conditional_policy"]("ca-m-test", {"tunnel"}, desired)


def test_conditional_policy_include_recursion_restores_parent_and_bounds_cycles(tmp_path):
    namespace = remote()
    config = tmp_path / "sshd_config"
    child = tmp_path / "conditional.conf"
    child.write_text("Match Address 192.0.2.0/24\nPermitOpen 127.0.0.1:8101\n")
    config.write_text(f"Include {child}\nAllowTcpForwarding yes\n")
    namespace["Path"] = lambda path: config if path == "/etc/ssh/sshd_config" else Path(path)
    desired, _ = namespace["desired_policy"]("ca-m-test", 8101)
    namespace["check_conditional_policy"]("ca-m-test", {"tunnel"}, desired)
    child.write_text("Match all\nAllowTCPForwarding =yes\n")
    config.write_text(f"Match User admin\nInclude {child}\n")
    namespace["check_conditional_policy"]("ca-m-test", {"tunnel"}, desired)
    config.write_text(f"Match Address 192.0.2.0/24\nInclude {child}\n")
    with pytest.raises(namespace["Blocked"], match="ssh_conditional_policy"):
        namespace["check_conditional_policy"]("ca-m-test", {"tunnel"}, desired)
    config.write_text(f"Include {config}\n")
    with pytest.raises(namespace["Blocked"], match="ssh_conditional_policy"):
        namespace["check_conditional_policy"]("ca-m-test", {"tunnel"}, desired)


def test_operator_admission_failure_happens_before_fragment_or_key_authorization():
    namespace = remote()
    namespace["ssh_policy"] = lambda _: {"allowusers": "admin"}
    namespace["user_groups"] = lambda _: {"tunnel"}
    writes = []
    namespace["atomic_write"] = lambda *args: writes.append(args)
    with pytest.raises(namespace["Blocked"], match="ssh_admission_blocked"):
        namespace["ensure_policy"]("ca-m-test", 8101)
    assert writes == []
