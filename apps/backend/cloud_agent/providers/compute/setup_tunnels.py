"""Install narrowly scoped tunnel credentials through existing verified SSH access."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import shlex
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import Field, SecretStr, ValidationError, field_validator

from cloud_agent.ports.external.compute_provider import ComputeValue, ProviderError

if TYPE_CHECKING:
    from cloud_agent.ports.external.compute_provider import SshConnection
    from cloud_agent.ports.external.host_bootstrap import SshTransport


class TunnelKey(ComputeValue):
    """A generated key whose private material is never rendered in diagnostics."""

    private_key: SecretStr
    public_key: str


class ControlPlaneEndpoint(ComputeValue):
    """Only the inspected ports and IPv4 bridge gateway needed by the Host."""

    main_port: int = Field(ge=1, le=65535, strict=True)
    mcp_port: int = Field(ge=1, le=65535, strict=True)
    gateway_ip: str
    machine_identity: str = Field(pattern=r"^[a-f0-9]{64}$")

    @field_validator("gateway_ip")
    @classmethod
    def validate_gateway(cls, value: str) -> str:
        """Require a private unicast IPv4 address, never a wildcard or loopback."""
        address = ipaddress.IPv4Address(value)
        ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        if not any(address in ipaddress.IPv4Network(network) for network in ranges):
            raise ValueError("The sandbox manager requires the control-plane Docker bridge gateway")
        if address.is_multicast or address.is_link_local:
            raise ValueError("The sandbox manager requires a private unicast Docker bridge gateway")
        return str(address)


def tunnel_username(setup_id: str, role: str) -> str:
    """Derive separate stable service accounts, never reuse a management account."""
    if role not in {"inference", "mcp", "control"}:
        raise ProviderError("setup_configuration", "Unknown tunnel role")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", setup_id):
        raise ProviderError("setup_configuration", "Invalid server setup identifier")
    return "ca-" + role[0] + "-" + hashlib.sha256(setup_id.encode()).hexdigest()[:16]


def generate_tunnel_key() -> TunnelKey:
    """Create a distinct Ed25519 key without invoking a shell or writing files."""
    key = Ed25519PrivateKey.generate()
    return TunnelKey(
        private_key=SecretStr(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.OpenSSH,
                serialization.NoEncryption(),
            ).decode("ascii")
        ),
        public_key=key.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode("ascii"),
    )


_BLOCKERS = {
    "control_plane_missing": "No running control plane matches this application's release",
    "control_plane_ambiguous": "Several control planes match this release; use a dedicated connection",
    "control_plane_ports": "The selected control-plane API and MCP ports are not uniquely published",
    "control_plane_network": "The control plane needs one shared private Docker bridge network",
    "service_unreachable": "A selected tunnel service is not listening on its loopback port",
    "forwarding_disabled": "The selected SSH account does not permit the required TCP forwarding",
    "authorized_keys_unsupported": "The selected SSH account must use .ssh/authorized_keys",
    "ssh_config_unsupported": "SSH must include /etc/ssh/sshd_config.d/*.conf for automatic tunnel setup",
    "ssh_policy_conflict": "An existing SSH policy prevents the required restricted tunnel settings",
    "ssh_admission_blocked": "Existing SSH account or public-key restrictions do not admit the generated tunnel accounts",
    "ssh_conditional_policy": "An existing conditional SSH policy may block or widen tunnel access from future compute hosts",
    "ssh_reload_failed": "SSH could not reload the validated tunnel configuration",
    "reverse_port_unavailable": "The selected sandbox manager reverse port is already in use on the control plane",
    "setup_key_conflict": "This setup already has different tunnel keys; retry its saved configuration",
    "unsafe_key_file": "The SSH account's authorized_keys must be a regular file in a safe .ssh directory",
    "setup_failed": "Automatic tunnel setup failed; verify SSH sudo access and service prerequisites",
}


class SshTunnelSetup:
    """Perform fixed remote operations; all deployment values travel as JSON stdin."""

    def __init__(self, *, transport: SshTransport) -> None:
        self._transport = transport

    async def _run(self, connection: SshConnection, params: dict[str, Any]) -> dict[str, Any]:
        output = await self._transport.run(
            connection,
            "sudo -n python3 -c " + shlex.quote(_REMOTE_SCRIPT),
            stdin=json.dumps(params, separators=(",", ":")).encode(),
            timeout=90,
        )
        try:
            result = json.loads(output)
            if not isinstance(result, dict) or result.get("ok") is not True:
                reason = result.get("reason") if isinstance(result, dict) else None
                if isinstance(reason, str) and reason in _BLOCKERS:
                    raise ProviderError(reason, _BLOCKERS[reason])
                raise ValueError("invalid response")
            return result
        except (ValueError, TypeError):
            raise ProviderError(
                "invalid_response", "Tunnel setup returned an invalid response"
            ) from None

    async def discover_control_plane(
        self, connection: SshConnection, deployment_sha: str
    ) -> ControlPlaneEndpoint:
        """Identify one release-matched Compose project without reading its environment."""
        if not re.fullmatch(r"[a-f0-9]{40}", deployment_sha):
            raise ProviderError("setup_configuration", "An exact release revision is required")
        result = await self._run(
            connection, {"action": "discover", "deployment_sha": deployment_sha}
        )
        try:
            return ControlPlaneEndpoint.model_validate(result.get("endpoint"))
        except ValidationError:
            raise ProviderError(
                "invalid_response", "Control-plane discovery returned an invalid endpoint"
            ) from None

    async def check_gateway(self, connection: SshConnection, port: int = 8080) -> str:
        """Check the inference listener and return the verified machine's hashed identity."""
        _validate_port(port)
        result = await self._run(connection, {"action": "check_gateway", "port": port})
        identity = result.get("machine_identity")
        if not isinstance(identity, str) or not re.fullmatch(r"[a-f0-9]{64}", identity):
            raise ProviderError(
                "invalid_response", "Gateway discovery returned an invalid machine identity"
            )
        return identity

    async def provision_gateway(
        self, connection: SshConnection, public_key: str, port: int, setup_id: str
    ) -> None:
        """Authorize just the inference destination under an idempotent setup marker."""
        _validate_port(port)
        _validate_identity(setup_id, public_key)
        await self._run(
            connection,
            {
                "action": "gateway",
                "port": port,
                "key": public_key,
                "setup_id": setup_id,
                "inference_username": tunnel_username(setup_id, "inference"),
            },
        )

    async def provision_control(
        self,
        connection: SshConnection,
        mcp_public_key: str,
        control_public_key: str,
        endpoint: ControlPlaneEndpoint,
        reverse_port: int,
        setup_id: str,
    ) -> None:
        """Authorize separate MCP/main keys and this Host's single sandbox manager reverse listener."""
        _validate_identity(setup_id, mcp_public_key)
        _validate_identity(setup_id, control_public_key)
        _validate_port(reverse_port)
        if reverse_port < 1024:
            raise ProviderError(
                "setup_configuration", "The sandbox manager requires an unprivileged reverse port"
            )
        if mcp_public_key == control_public_key or endpoint.main_port == endpoint.mcp_port:
            raise ProviderError(
                "setup_configuration", "Main API and MCP require separate keys and ports"
            )
        if reverse_port in {endpoint.main_port, endpoint.mcp_port}:
            raise ProviderError(
                "setup_configuration", "The sandbox manager needs a separate reverse port"
            )
        await self._run(
            connection,
            {
                "action": "control",
                "mcp_key": mcp_public_key,
                "control_key": control_public_key,
                "endpoint": endpoint.model_dump(),
                "reverse_port": reverse_port,
                "setup_id": setup_id,
                "mcp_username": tunnel_username(setup_id, "mcp"),
                "control_username": tunnel_username(setup_id, "control"),
            },
        )


def _validate_port(port: int) -> None:
    if type(port) is not int or not 1 <= port <= 65535:
        raise ProviderError("setup_configuration", "Tunnel ports must be between 1 and 65535")


def _validate_identity(setup_id: str, public_key: str) -> None:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", setup_id):
        raise ProviderError("setup_configuration", "Invalid server setup identifier")
    if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}", public_key):
        raise ProviderError("setup_configuration", "Generated Ed25519 tunnel keys are required")
    try:
        key = serialization.load_ssh_public_key(public_key.encode("ascii"))
    except (ValueError, TypeError):
        raise ProviderError("setup_configuration", "Invalid Ed25519 tunnel public key") from None
    if not isinstance(key, Ed25519PublicKey):
        raise ProviderError("setup_configuration", "Generated Ed25519 tunnel keys are required")


# This script is trusted application code. It never reads Docker Config.Env,
# accepts commands from stdin, prints subprocess diagnostics, or returns keys.
_REMOTE_SCRIPT = r"""
import fcntl
import glob
import grp
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile

class Blocked(Exception):
    pass

def refuse(reason):
    raise Blocked(reason)

def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=20)
    if result.returncode:
        refuse('setup_failed')
    return result.stdout

def document(args):
    return json.loads(command(args))

def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=3):
            pass
    except OSError:
        refuse('service_unreachable')

def machine_identity():
    identity = Path('/etc/machine-id').read_text().strip()
    if len(identity) != 32 or any(character not in '0123456789abcdef' for character in identity):
        refuse('setup_failed')
    return hashlib.sha256(identity.encode()).hexdigest()

def inspect_container(identity):
    template = ('{"id":{{json .Id}},"image":{{json .Image}},'
                '"labels":{{json .Config.Labels}},"ports":{{json .NetworkSettings.Ports}},'
                '"networks":{{json .NetworkSettings.Networks}}}')
    return document(['docker', 'container', 'inspect', '--format', template, identity])

def image_revision(image):
    labels = document(['docker', 'image', 'inspect', '--format', '{{json .Config.Labels}}', image])
    return (labels or {}).get('org.opencontainers.image.revision')

def published_port(container, internal):
    ports = (container.get('ports') or {}).get(str(internal) + '/tcp') or []
    values = {int(p['HostPort']) for p in ports if p.get('HostIp') in ('127.0.0.1', '0.0.0.0')}
    if len(values) != 1 or len({int(p['HostPort']) for p in ports}) != 1:
        refuse('control_plane_ports')
    port = values.pop()
    if not 1 <= port <= 65535:
        refuse('control_plane_ports')
    return port

def discover(params):
    candidates = []
    ids = command(['docker', 'container', 'ls', '--filter', 'label=com.docker.compose.service=control-plane', '--format', '{{.ID}}']).split()
    for identity in ids:
        candidate = inspect_container(identity)
        labels = candidate.get('labels') or {}
        revision = labels.get('io.hybridinference.deployment.sha') or image_revision(candidate['image'])
        if revision == params['deployment_sha'] and labels.get('com.docker.compose.project'):
            candidates.append(candidate)
    if not candidates:
        refuse('control_plane_missing')
    if len(candidates) != 1:
        refuse('control_plane_ambiguous')
    main = candidates[0]
    project = main['labels']['com.docker.compose.project']
    relay_ids = command(['docker', 'container', 'ls', '--filter', 'label=com.docker.compose.service=mcp-relay', '--filter', 'label=com.docker.compose.project=' + project, '--format', '{{.ID}}']).split()
    if len(relay_ids) != 1:
        refuse('control_plane_missing')
    relay = inspect_container(relay_ids[0])
    if main['image'] != relay['image']:
        refuse('control_plane_missing')
    main_port, mcp_port = published_port(main, 8000), published_port(relay, 8001)
    if main_port == mcp_port:
        refuse('control_plane_ports')
    gateways = set()
    for network in set(main['networks'] or {}) & set(relay['networks'] or {}):
        data = document(['docker', 'network', 'inspect', '--format', '{{json .}}', network])
        labels = data.get('Labels') or {}
        if (data.get('Driver') != 'bridge' or labels.get('com.docker.compose.project') != project
                or labels.get('com.docker.compose.network') != 'control-plane'):
            continue
        for subnet in (data.get('IPAM') or {}).get('Config') or []:
            try:
                address = ipaddress.IPv4Address(subnet.get('Gateway', ''))
                network_range = ipaddress.IPv4Network(subnet.get('Subnet', ''))
            except ValueError:
                continue
            private_ranges = ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
            if (address in network_range and any(address in ipaddress.IPv4Network(value)
                                                for value in private_ranges)):
                gateways.add(str(address))
    if len(gateways) != 1:
        refuse('control_plane_network')
    listening(main_port)
    listening(mcp_port)
    return {'main_port': main_port, 'mcp_port': mcp_port, 'gateway_ip': gateways.pop(),
            'machine_identity': machine_identity()}

def ssh_policy(username):
    text = command(['/usr/sbin/sshd', '-T', '-C', 'user=' + username + ',host=localhost,addr=127.0.0.1'])
    policy = {}
    lists = {'allowusers', 'denyusers', 'allowgroups', 'denygroups'}
    for line in text.splitlines():
        if ' ' not in line:
            continue
        keyword, value = line.split(' ', 1)
        policy[keyword] = ((policy[keyword] + ' ' + value)
                           if keyword in lists and keyword in policy else value)
    return policy

def pattern_matches(value, pattern):
    # OpenSSH patterns support '*' and '?', not filesystem character classes.
    expression = re.escape(pattern).replace(r'\*', '.*').replace(r'\?', '.')
    return re.fullmatch(expression, value) is not None

def pattern_list_matches(values, patterns):
    matched = False
    for pattern in patterns.split(','):
        excluded = pattern.startswith('!')
        candidate = pattern[1:] if excluded else pattern
        if any(pattern_matches(value, candidate) for value in values):
            if excluded:
                return False
            matched = True
    return matched

def user_groups(username):
    user = pwd.getpwnam(username)
    return {grp.getgrgid(identity).gr_name for identity in os.getgrouplist(username, user.pw_gid)}

def check_admission(username, groups, policy):
    # A future compute host's address is not known during setup. Only an
    # unconditional username allow can prove admission; a possibly applicable
    # User@Host denial must block instead of relying on the localhost probe.
    allowed = policy.get('allowusers', '').split()
    denied = policy.get('denyusers', '').split()
    if allowed and not any('@' not in item and pattern_matches(username, item) for item in allowed):
        refuse('ssh_admission_blocked')
    if any(pattern_matches(username, item.split('@', 1)[0]) for item in denied):
        refuse('ssh_admission_blocked')
    allowed_groups = policy.get('allowgroups', '').split()
    denied_groups = policy.get('denygroups', '').split()
    if allowed_groups and not any(pattern_matches(group, item) for group in groups for item in allowed_groups):
        refuse('ssh_admission_blocked')
    if any(pattern_matches(group, item) for group in groups for item in denied_groups):
        refuse('ssh_admission_blocked')
    if ('pubkeyacceptedalgorithms' in policy
            and 'ssh-ed25519' not in policy['pubkeyacceptedalgorithms'].split(',')):
        refuse('ssh_admission_blocked')

def match_context(arguments, username, groups):
    applicable, conditional = True, False
    index = 0
    while index < len(arguments):
        criterion = arguments[index].lower()
        index += 1
        if criterion == 'all':
            continue
        if criterion == 'invalid-user':
            applicable = False
            continue
        if index >= len(arguments):
            refuse('ssh_conditional_policy')
        value = arguments[index]
        index += 1
        if criterion == 'user':
            applicable = applicable and pattern_list_matches({username}, value)
        elif criterion == 'group':
            # OpenSSH ga_match_pattern_list makes a negated match in any
            # membership veto positives in all other memberships.
            applicable = applicable and pattern_list_matches(groups, value)
        else:
            # Address, Host, LocalAddress, LocalPort and any future criterion
            # may differ when the actual Host opens its tunnel connection.
            conditional = True
    return applicable, conditional

def check_conditional_policy(username, groups, desired):
    # Parse the trusted configuration's Include tree, preserving the parent's
    # Match context exactly as OpenSSH does. Do not try to prove arbitrary
    # network conditions by probing a finite set of sample addresses.
    limits = {'files': 0, 'bytes': 0}
    admission = {'allowusers', 'denyusers', 'allowgroups', 'denygroups', 'pubkeyacceptedalgorithms'}

    def inspect(path, inherited=(True, False), depth=0):
        if depth > 16 or limits['files'] >= 128:
            refuse('ssh_conditional_policy')
        limits['files'] += 1
        content = path.read_text()
        limits['bytes'] += len(content)
        if limits['bytes'] > 1024 * 1024:
            refuse('ssh_conditional_policy')
        context = inherited
        for line in content.splitlines():
            try:
                arguments = shlex.split(line, comments=True)
            except ValueError:
                refuse('ssh_conditional_policy')
            if not arguments:
                continue
            if '=' in arguments[0]:
                keyword, value = arguments[0].split('=', 1)
                arguments = [keyword, value, *arguments[1:]]
            keyword = arguments[0].lower()
            if keyword == 'challengeresponseauthentication':
                keyword = 'kbdinteractiveauthentication'
            if keyword == 'pubkeyacceptedkeytypes':
                keyword = 'pubkeyacceptedalgorithms'
            values = arguments[1:]
            if values and values[0].startswith('='):
                values[0] = values[0][1:]
                if not values[0]:
                    values = values[1:]
            if keyword == 'match':
                applicable, conditional = match_context(values, username, groups)
                context = (inherited[0] and applicable, inherited[1] or conditional)
            elif keyword == 'include':
                for value in values:
                    pattern = os.path.expanduser(value)
                    if not os.path.isabs(pattern):
                        pattern = '/etc/ssh/' + pattern
                    for included in sorted(glob.glob(pattern)):
                        inspect(Path(included), context, depth + 1)
            elif context[0] and context[1]:
                if keyword in admission or (keyword in desired and ' '.join(values) != desired[keyword]):
                    refuse('ssh_conditional_policy')

    inspect(Path('/etc/ssh/sshd_config'))

def desired_policy(username, port, reverse=None):
    values = {
        'allowtcpforwarding': 'yes' if reverse else 'local',
        'disableforwarding': 'no',
        'allowstreamlocalforwarding': 'no',
        'permitopen': '127.0.0.1:' + str(port),
        'permitlisten': reverse or 'none',
        'gatewayports': 'clientspecified' if reverse else 'no',
        'passwordauthentication': 'no',
        'kbdinteractiveauthentication': 'no',
        'pubkeyauthentication': 'yes',
        'authenticationmethods': 'publickey',
        'authorizedkeysfile': '.ssh/authorized_keys',
        'forcecommand': '/usr/sbin/nologin',
        'allowagentforwarding': 'no',
        'x11forwarding': 'no',
        'permittty': 'no',
        'permituserrc': 'no',
        'permittunnel': 'no',
    }
    content = '# Managed by Cloud Agent server setup\nMatch User ' + username + '\n'
    content += ''.join('    ' + key + ' ' + value + '\n' for key, value in values.items())
    content += 'Match all\n'
    return values, content

def validate_reload_policy(username, groups, desired):
    # sshd -T inspects files on disk, not the running daemon. Even an exact
    # disk match can be an interrupted write that has never been activated.
    command(['/usr/sbin/sshd', '-t'])
    active = ssh_policy(username)
    check_admission(username, groups, active)
    if not all(active.get(key) == value for key, value in desired.items()):
        refuse('ssh_policy_conflict')
    for service in ('ssh', 'sshd'):
        result = subprocess.run(['systemctl', 'reload', service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        if result.returncode == 0:
            return
    refuse('ssh_reload_failed')

def ensure_policy(username, port, reverse=None):
    desired, content = desired_policy(username, port, reverse)
    policy = ssh_policy(username)
    groups = user_groups(username)
    check_admission(username, groups, policy)
    check_conditional_policy(username, groups, desired)
    if all(policy.get(key) == value for key, value in desired.items()):
        validate_reload_policy(username, groups, desired)
        return
    directory = Path('/etc/ssh/sshd_config.d')
    if not directory.is_dir() or directory.is_symlink():
        refuse('ssh_config_unsupported')
    path = directory / ('00-cloud-agent-tunnels-' + username + '.conf')
    if path.is_symlink():
        refuse('ssh_config_unsupported')
    previous = path.read_bytes() if path.exists() else None
    try:
        atomic_write(path, content, 0, 0, 0o600)
        validate_reload_policy(username, groups, desired)
    except BaseException:
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, previous.decode(), 0, 0, 0o600)
        raise

def atomic_write(path, content, uid, gid, mode):
    descriptor, temporary = tempfile.mkstemp(prefix='.cloud-agent-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            os.fchown(stream.fileno(), uid, gid)
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def key_path(username):
    base = Path('/var/lib/cloud-agent-tunnels')
    if base.is_symlink():
        refuse('unsafe_key_file')
    base.mkdir(mode=0o755, exist_ok=True)
    info = base.stat()
    if info.st_uid != 0 or info.st_mode & 0o022:
        refuse('unsafe_key_file')
    home = base / username
    try:
        user = pwd.getpwnam(username)
    except KeyError:
        command(['useradd', '--system', '--create-home', '--home-dir', str(home),
                 '--shell', '/usr/sbin/nologin', '--password', '*', username])
        user = pwd.getpwnam(username)
    if user.pw_dir != str(home) or user.pw_shell != '/usr/sbin/nologin' or user.pw_uid == 0:
        refuse('setup_key_conflict')
    if home.is_symlink():
        refuse('unsafe_key_file')
    directory = Path(user.pw_dir) / '.ssh'
    if directory.is_symlink():
        refuse('unsafe_key_file')
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.stat()
    if info.st_uid not in (0, user.pw_uid) or info.st_mode & 0o022:
        refuse('unsafe_key_file')
    os.chown(directory, user.pw_uid, user.pw_gid)
    path = directory / 'authorized_keys'
    if path.is_symlink():
        refuse('unsafe_key_file')
    if path.exists():
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or info.st_uid not in (0, user.pw_uid) or info.st_mode & 0o022):
            refuse('unsafe_key_file')
    return path, user

def entries(params):
    prefix = 'command="",restrict,port-forwarding,'
    tag = ' cloud-agent-setup:' + params['setup_id'] + ':'
    if params['action'] == 'gateway':
        return [prefix + 'permitopen="127.0.0.1:' + str(params['port']) + '" ' + params['key'] + tag + 'inference']
    endpoint = params['endpoint']
    return [
        prefix + 'permitopen="127.0.0.1:' + str(endpoint['mcp_port']) + '" ' + params['mcp_key'] + tag + 'mcp',
        prefix + 'permitopen="127.0.0.1:' + str(endpoint['main_port']) + '",permitlisten="' + endpoint['gateway_ip'] + ':' + str(params['reverse_port']) + '" ' + params['control_key'] + tag + 'control',
    ]

def provision_account(username, entry, port, reverse=None):
    path, user = key_path(username)
    current = path.read_text() if path.exists() else ''
    if current.strip() and current.splitlines() != [entry]:
        refuse('setup_key_conflict')
    already = current.splitlines() == [entry]
    if reverse and not already:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            address, reverse_port = reverse.rsplit(':', 1)
            probe.bind((address, int(reverse_port)))
        except OSError:
            refuse('reverse_port_unavailable')
        finally:
            probe.close()
    ensure_policy(username, port, reverse)
    if not already:
        atomic_write(path, entry + '\n', user.pw_uid, user.pw_gid, 0o600)

def provision(params):
    expected = entries(params)
    if params['action'] == 'gateway':
        provision_account(params['inference_username'], expected[0], params['port'])
    else:
        endpoint = params['endpoint']
        provision_account(params['mcp_username'], expected[0], endpoint['mcp_port'])
        reverse = endpoint['gateway_ip'] + ':' + str(params['reverse_port'])
        provision_account(params['control_username'], expected[1], endpoint['main_port'], reverse)

def main():
    try:
        params = json.load(sys.stdin)
        action = params['action']
        if action == 'discover':
            result = {'ok': True, 'endpoint': discover(params)}
        elif action == 'check_gateway':
            listening(params['port'])
            result = {'ok': True, 'machine_identity': machine_identity()}
        elif action in ('gateway', 'control'):
            descriptor = os.open('/run/lock/cloud-agent-server-setup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as lock:
                info = os.fstat(lock.fileno())
                if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    refuse('setup_failed')
                fcntl.flock(lock, fcntl.LOCK_EX)
                if action == 'gateway':
                    listening(params['port'])
                else:
                    listening(params['endpoint']['main_port'])
                    listening(params['endpoint']['mcp_port'])
                provision(params)
            result = {'ok': True}
        else:
            refuse('setup_failed')
    except Blocked as error:
        result = {'ok': False, 'reason': str(error)}
    except Exception:
        result = {'ok': False, 'reason': 'setup_failed'}
    print(json.dumps(result))

if __name__ == '__main__':
    main()
"""
