"""Deploy the reviewed host recipe over SSH; do not reproduce its security checks.

The first supported bootstrap OS is Ubuntu 22.04/24.04 amd64 with passwordless
sudo and KVM. Docker/Compose and pinned Kata can be installed by the trusted
profile. Tunnel permissions and broker routing must already be provisioned on
the stable control-plane/gateway machines and supplied through the profile.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shlex
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values

from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.ports.external.host_bootstrap import (
    BootstrapResult,
    HostInventory,
    PreflightResult,
)

if TYPE_CHECKING:
    from pydantic import SecretStr

    from cloud_agent.ports.external.compute_provider import (
        NodeObservation,
        SecretResolver,
        SshConnection,
    )
    from cloud_agent.ports.external.host_bootstrap import (
        BootstrapIntent,
        DeploymentProfile,
        SshTransport,
    )


_PREFLIGHT = r"""
import hashlib, json, os, platform, shutil, subprocess
from pathlib import Path
osinfo = {}
for line in Path('/etc/os-release').read_text().splitlines():
    key, _, value = line.partition('=')
    osinfo[key] = value.strip('"')
def works(command):
    return subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
root = os.geteuid() == 0 or (shutil.which('sudo') and works(['sudo', '-n', 'true']))
checks = {
    'supported_os': osinfo.get('ID') == 'ubuntu' and osinfo.get('VERSION_ID') in ('22.04', '24.04'),
    'architecture': platform.machine() == 'x86_64',
    'privileged_deploy': bool(root),
    'kvm': Path('/dev/kvm').exists(),
    'docker': bool(shutil.which('docker')),
    'compose': bool(shutil.which('docker')) and works(['sudo', '-n', 'docker', 'compose', 'version']),
}
machine = Path('/etc/machine-id').read_text().strip().lower()
print(json.dumps({'machine_identity': hashlib.sha256(machine.encode()).hexdigest(),
    'operating_system': osinfo.get('ID', '') + ':' + osinfo.get('VERSION_ID', ''),
    'architecture': platform.machine(), 'checks': checks}))
"""

# Executed with sudo and a tar stream on stdin, never a secret command argument.
# Existing installations require our persisted identity marker. A retry uses
# the same identity and flock; it cannot overwrite or enroll a different Host.
_DEPLOY = r"""
import fcntl, io, json, os, subprocess, sys, tarfile
from pathlib import Path
params = json.loads(sys.argv[1])
root = Path(params['root'])
identity = params['identity']
root.parent.mkdir(parents=True, exist_ok=True)
if root.is_symlink() or root.resolve() != root:
    raise SystemExit('unsafe deployment root')
lock_path = root.parent / ('.' + root.name + '.compute-lock')
if lock_path.is_symlink():
    raise SystemExit('unsafe lock')
with open(lock_path, 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    marker = root / '.compute-identity.json'
    if root.exists() and any(root.iterdir()):
        if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != identity:
            raise SystemExit('existing deployment identity conflict')
    root.mkdir(mode=0o700, exist_ok=True)
    root.chmod(0o700)
    marker.write_text(json.dumps(identity))
    marker.chmod(0o600)
    with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()), mode='r:gz') as archive:
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if member.name.startswith('/') or '..' in parts or not (member.isfile() or member.isdir()):
                raise SystemExit('invalid deployment archive')
            target = root / member.name
            # Never traverse a link from an earlier, unexpected remote change.
            if target.resolve() != target or target.is_symlink():
                raise SystemExit('unsafe existing deployment path')
        archive.extractall(root, filter='data')
    environment = os.environ.copy()
    environment.update({'DEBIAN_FRONTEND': 'noninteractive', 'AGENT_DEPLOYMENT_SHA': params['sha']})
    def run(step, args):
        result = subprocess.run(args, cwd=root, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            print(json.dumps({'ready': False, 'blocked_reasons': [step]}))
            raise SystemExit(0)
    if params['install']:
        run('prerequisites_update_failed', ['apt-get', 'update'])
        run('prerequisites_install_failed', ['apt-get', 'install', '-y', 'docker.io', 'docker-compose-v2', 'docker-buildx', 'curl', 'ca-certificates', 'xz-utils', 'zstd', 'nftables'])
        run('docker_start_failed', ['systemctl', 'enable', '--now', 'docker'])
        # The bundled script validates its pinned download checksum and KVM.
        run('kata_install_failed', ['bash', 'deploy/host/setup_kata_runtime.sh'])
    # This invokes the existing Kata, relay, broker, observer, network-guard
    # and retained-state checks, then writes the actual shared-ready marker.
    run('host_deployment_failed', ['bash', 'deploy/host/sandbox_host.sh', 'up'])
    run('host_readiness_failed', ['bash', 'deploy/host/sandbox_host.sh', 'check'])
    (root / '.deployed-sha').write_text(params['sha'] + '\n')
    # Leave the persisted host credential in place on retries. Remove the
    # spent bootstrap token from .env; it never leaves through command output.
    envfile = root / '.env'
    envfile.write_text('\n'.join(line for line in envfile.read_text().splitlines() if not line.startswith('AGENT_ENROLL_TOKEN=')) + '\n')
    print(json.dumps({'ready': True, 'blocked_reasons': []}))
"""

_READINESS = r"""
import json, os, subprocess, sys
from pathlib import Path
params = json.loads(sys.argv[1])
root = Path(params['root'])
marker = root / '.compute-identity.json'
ready = False
if marker.is_file() and not marker.is_symlink() and json.loads(marker.read_text()) == params['identity']:
    environment = os.environ.copy()
    environment['AGENT_DEPLOYMENT_SHA'] = params['sha']
    result = subprocess.run(['bash', 'deploy/host/sandbox_host.sh', 'check'], cwd=root, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ready = result.returncode == 0 and (root / '.shared-live-ready').read_text().strip() == params['sha']
print(json.dumps({'ready': ready, 'blocked_reasons': [] if ready else ['host_readiness_failed']}))
"""

_REQUIRED_ENV = {
    "AGENT_SANDBOX_IMAGE",
    "AGENT_WORKSPACE_BROKER_TOKEN",
    "AGENT_TUNNEL_SSH_DESTINATION",
    "AGENT_TUNNEL_SSH_KEY",
    "AGENT_TUNNEL_SSH_KNOWN_HOSTS",
    "AGENT_MCP_TUNNEL_SSH_DESTINATION",
    "AGENT_MCP_TUNNEL_SSH_KEY",
    "AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS",
    "AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION",
    "AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY",
    "AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS",
    "AGENT_BROKER_REVERSE_BIND",
    "AGENT_BROKER_REVERSE_PORT",
    "AGENT_WORKSPACE_BROKER_ADVERTISE_URL",
    "AGENT_EGRESS_CHAT_NETWORK_POOL",
    "AGENT_EGRESS_DNS_SERVER",
}
# Host configuration has its own namespace. Unrelated service/process variables
# are not forwarded even when an operator accidentally includes them in a file.
_ALLOWED_NON_AGENT_ENV = {
    "COMPOSE_PROJECT",
    "SIBLING_COMPOSE_PROJECTS",
    "SHARED_READY_TIMEOUT",
    "CLAUDE_CODE_EXTRA_VERSIONS",
    "CODEX_EXTRA_VERSIONS",
    "PI_EXTRA_VERSIONS",
    "OPENCODE_EXTRA_VERSIONS",
    "KILO_EXTRA_VERSIONS",
}
_FORBIDDEN_ENV = {
    "AGENT_COMPUTE_SECRET_KEY",
    "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY",
    "AGENT_CONTROL_TOKEN_SECRET",
    "AGENT_SESSION_SECRET",
    "AGENT_SANDBOX_ALLOW_OPEN_NETWORK",
    "AGENT_EGRESS_AGENT_TIER",
    "ALLOW_UNSAFE_CONTAINER_BACKEND",
}


class SshHostBootstrap:
    """Install and verify the existing host deployment recipe through SSH."""

    def __init__(self, *, transport: SshTransport, resolver: SecretResolver) -> None:
        self._transport = transport
        self._resolver = resolver

    @staticmethod
    def _connection(node: NodeObservation) -> SshConnection:
        if node.connection is None:
            raise ProviderError(
                "ssh_configuration", "This node has no verified SSH connection configuration"
            )
        return node.connection

    @staticmethod
    def _json(text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
        except (ValueError, UnicodeError):
            raise ProviderError(
                "invalid_response", "Remote deployment returned an invalid result"
            ) from None
        if not isinstance(value, dict):
            raise ProviderError("invalid_response", "Remote deployment returned an invalid result")
        return value

    async def preflight(self, node: NodeObservation, profile: DeploymentProfile) -> PreflightResult:
        """Verify machine identity and prerequisites without installing software."""
        text = await self._transport.run(
            self._connection(node), "python3 -c " + shlex.quote(_PREFLIGHT)
        )
        result = self._json(text)
        checks = result.get("checks", {})
        required = {"supported_os", "architecture", "privileged_deploy", "kvm"}
        if not profile.install_prerequisites:
            required |= {"docker", "compose"}
        blocked = [name for name in sorted(required) if checks.get(name) is not True]
        if node.machine_identity and result.get("machine_identity") != node.machine_identity:
            blocked.append("machine_identity_changed")
        return PreflightResult(
            ready=not blocked,
            machine_identity=str(result.get("machine_identity", "")),
            operating_system=str(result.get("operating_system", "")),
            architecture=str(result.get("architecture", "")),
            checks=checks,
            blocked_reasons=tuple(blocked),
        )

    @staticmethod
    def _params(profile: DeploymentProfile, intent: BootstrapIntent) -> dict[str, Any]:
        return {
            "root": profile.deploy_root,
            "sha": profile.deployment_sha,
            "install": profile.install_prerequisites,
            "identity": {
                "allocation_id": intent.allocation_id,
                "node_id": intent.node_id,
                "host": intent.host,
                "deployment_sha": profile.deployment_sha,
            },
        }

    async def _archive(
        self,
        profile: DeploymentProfile,
        intent: BootstrapIntent,
        enrollment_token: SecretStr | None,
    ) -> bytes:
        try:
            source = Path(profile.source_bundle).read_bytes()
        except OSError:
            raise ProviderError(
                "deployment_profile_invalid", "Trusted deployment bundle is unavailable"
            ) from None
        if hashlib.sha256(source).hexdigest() != profile.source_sha256:
            raise ProviderError(
                "deployment_profile_invalid",
                "Trusted deployment bundle digest differs from its profile",
            )
        if not profile.environment_ref.startswith("deployment:") or any(
            not ref.startswith("deployment:") for ref in profile.files.values()
        ):
            raise ProviderError(
                "deployment_profile_invalid",
                "Deployment inputs must use dedicated deployment secrets",
            )
        template = await self._resolver.resolve(profile.environment_ref)
        for key, value in {
            "host": intent.host,
            "node_id": intent.node_id,
            "allocation_id": intent.allocation_id,
            "deploy_root": profile.deploy_root,
        }.items():
            template = template.replace("{" + key + "}", value)
        values = dotenv_values(stream=io.StringIO(template), interpolate=False)
        if (
            any(
                not name.startswith("AGENT_") and name not in _ALLOWED_NON_AGENT_ENV
                for name in values
            )
            or any(name in values for name in _FORBIDDEN_ENV)
            or values.get("AGENT_SANDBOX_BACKEND", "kata") != "kata"
        ):
            raise ProviderError(
                "deployment_profile_invalid",
                "Deployment profile violates the approved host security boundary",
            )
        if any(not values.get(name) for name in _REQUIRED_ENV):
            raise ProviderError(
                "deployment_profile_invalid",
                "Deployment profile is missing required relay, broker or network settings",
            )
        values.update(
            {
                "COMPOSE_PROJECT": "ca-" + intent.node_id.replace("-", "").lower(),
                "AGENT_RUNNER_HOST": intent.host,
                "AGENT_HOST_STATE_ROOT": "/var/lib/cloud-agent/" + intent.host,
                "AGENT_DEPLOYMENT_SHA": profile.deployment_sha,
                "AGENT_SANDBOX_BACKEND": "kata",
            }
        )
        # A persisted runner credential takes precedence in the existing host
        # recipe. A retry never deletes that file to spend another token.
        if enrollment_token is not None:
            values["AGENT_ENROLL_TOKEN"] = enrollment_token.get_secret_value()
        else:
            values.pop("AGENT_ENROLL_TOKEN", None)
        for key, environment_value in values.items():
            if (
                not re.fullmatch(r"[A-Z][A-Z0-9_]*", key)
                or environment_value is None
                or any(c in environment_value for c in ("\n", "\r", "'", "\x00"))
            ):
                raise ProviderError(
                    "deployment_profile_invalid",
                    "Deployment environment must contain single-line values",
                )
        # Compose's single quotes disable interpolation; shell metacharacters
        # and dollar signs remain literal, never code or an environment lookup.
        env = "".join(f"{key}='{value}'\n" for key, value in values.items()).encode()
        assets: dict[str, bytes] = {".env": env}
        for name, ref in profile.files.items():
            if not name.startswith(".compute-secrets/"):
                raise ProviderError(
                    "deployment_profile_invalid",
                    "Deployed credential files must be inside .compute-secrets/",
                )
            assets[name] = (await self._resolver.resolve(ref)).encode()
        for prefix in ("AGENT_TUNNEL", "AGENT_MCP_TUNNEL", "AGENT_CONTROL_PLANE_TUNNEL"):
            for suffix in ("SSH_KEY", "SSH_KNOWN_HOSTS"):
                path = values[f"{prefix}_{suffix}"] or ""
                relative = path.removeprefix(profile.deploy_root + "/")
                if relative not in assets or not relative.startswith(".compute-secrets/"):
                    raise ProviderError(
                        "deployment_profile_invalid",
                        "Relay credentials must refer to this profile's dedicated deployed files",
                    )
        output = io.BytesIO()
        try:
            with (
                tarfile.open(fileobj=io.BytesIO(source), mode="r:*") as archive,
                tarfile.open(fileobj=output, mode="w:gz") as payload,
            ):
                found = set()
                for member in archive.getmembers():
                    name = member.name.removeprefix("./")
                    if not name or name == ".":
                        continue
                    if (
                        name.startswith("/")
                        or ".." in name.split("/")
                        or not (member.isdir() or member.isfile())
                    ):
                        raise ProviderError(
                            "deployment_profile_invalid",
                            "Deployment bundle contains an unsafe path",
                        )
                    if name in {".env", ".compute-identity.json"} or name.startswith(
                        (".compute-secrets/", ".git/")
                    ):
                        raise ProviderError(
                            "deployment_profile_invalid",
                            "Deployment bundle contains private deployment state",
                        )
                    if name == ".dockerignore":
                        original = archive.extractfile(member)
                        ignored = (
                            original.read() if original else b""
                        ) + b"\n.compute-secrets\n.compute-identity.json\n"
                        member.size = len(ignored)
                        member.name = name
                        payload.addfile(member, io.BytesIO(ignored))
                        found.add(name)
                        continue
                    member.name = name
                    found.add(name)
                    payload.addfile(
                        member, archive.extractfile(member) if member.isfile() else None
                    )
                required_files = {
                    "deploy/host/sandbox_host.sh",
                    "deploy/host/setup_kata_runtime.sh",
                    "deploy/host/docker-compose.sandbox.yml",
                    "pyproject.toml",
                }
                if not required_files <= found:
                    raise ProviderError(
                        "deployment_profile_invalid",
                        "Deployment bundle does not contain the host recipe",
                    )
                if ".dockerignore" not in found:
                    assets[".dockerignore"] = b".env\n.compute-secrets\n.compute-identity.json\n"
                for name, data in assets.items():
                    member = tarfile.TarInfo(name)
                    member.size, member.mode = len(data), 0o600
                    payload.addfile(member, io.BytesIO(data))
        except (tarfile.TarError, OSError):
            raise ProviderError(
                "deployment_profile_invalid", "Trusted deployment bundle could not be read"
            ) from None
        return output.getvalue()

    async def deploy(
        self,
        node: NodeObservation,
        profile: DeploymentProfile,
        intent: BootstrapIntent,
        enrollment_token: SecretStr | None,
    ) -> BootstrapResult:
        """Install the trusted host recipe while retaining its existing safety gates."""
        preflight = await self.preflight(node, profile)
        if not preflight.ready:
            return BootstrapResult(
                ready=False,
                host=intent.host,
                deployment_sha=profile.deployment_sha,
                blocked_reasons=preflight.blocked_reasons,
            )
        archive = await self._archive(profile, intent, enrollment_token)
        command = (
            "sudo -n python3 -c "
            + shlex.quote(_DEPLOY)
            + " "
            + shlex.quote(json.dumps(self._params(profile, intent)))
        )
        result = self._json(
            await self._transport.run(
                self._connection(node), command, stdin=archive, timeout=profile.timeout_seconds
            )
        )
        return BootstrapResult(
            ready=result.get("ready") is True,
            host=intent.host,
            deployment_sha=profile.deployment_sha,
            blocked_reasons=tuple(result.get("blocked_reasons", ())),
        )

    async def readiness(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> BootstrapResult:
        """Recheck deployment identity and the full host and broker readiness gates."""
        command = (
            "sudo -n python3 -c "
            + shlex.quote(_READINESS)
            + " "
            + shlex.quote(json.dumps(self._params(profile, intent)))
        )
        result = self._json(
            await self._transport.run(
                self._connection(node), command, timeout=profile.timeout_seconds
            )
        )
        return BootstrapResult(
            ready=result.get("ready") is True,
            host=intent.host,
            deployment_sha=profile.deployment_sha,
            blocked_reasons=tuple(result.get("blocked_reasons", ())),
        )

    async def inventory(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> HostInventory:
        """Read complete Docker and retained state without deleting or adopting it."""
        params = self._params(profile, intent)
        script = (
            "sudo -n python3 -c " + shlex.quote(_INVENTORY) + " " + shlex.quote(json.dumps(params))
        )
        result = self._json(await self._transport.run(self._connection(node), script, timeout=60))
        return HostInventory.model_validate(result)


_INVENTORY = r"""
import json, shutil, subprocess, sys
from pathlib import Path
params = json.loads(sys.argv[1])
root = Path(params['root'])
marker = root / '.compute-identity.json'
result = {'complete': False, 'retained_containers': 0, 'unknown_containers': 0, 'orphan_state': False, 'blocked_reasons': []}
if root.exists() and any(root.iterdir()) and (not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != params['identity']):
    result['blocked_reasons'].append('deployment_identity_conflict')
elif not shutil.which('docker'):
    result['orphan_state'] = any(Path(p).exists() for p in ('/var/lib/docker', '/var/lib/containerd', '/var/lib/cloud-agent', '/var/lib/hybridinference'))
    result['complete'] = not result['orphan_state']
else:
    def output(args):
        return subprocess.check_output(args, stderr=subprocess.DEVNULL, text=True).strip()
    try:
        ids = output(['docker', 'ps', '-aq']).split()
        containers = json.loads(output(['docker', 'inspect', *ids])) if ids else []
        project = 'ca-' + params['identity']['node_id'].replace('-', '').lower()
        for container in containers:
            labels = (container.get('Config') or {}).get('Labels') or {}
            if labels.get('org.freeinference.chat-sandbox') == 'true':
                result['retained_containers'] += 1
            elif labels.get('com.docker.compose.project') != project:
                result['unknown_containers'] += 1
        networks = output(['docker', 'network', 'ls', '-q', '--filter', 'label=org.freeinference.chat-sandbox-network=true'])
        volumes = output(['docker', 'volume', 'ls', '-q'])
        state_root = Path('/var/lib/cloud-agent')
        state = state_root / params['identity']['host']
        # Only this exact deployment's enrollment credential and flock file
        # are inert. Another host, nested same-named file, or a symlink can
        # hide retained state and must never be excused by its basename.
        inert = {state / 'credential', state / '.enroll.lock'}
        paths = list(state_root.rglob('*')) if state_root.exists() else []
        unexpected = state_root.is_symlink() or any(p.is_symlink() or (not p.is_dir() and p not in inert) for p in paths)
        legacy = Path('/var/lib/hybridinference')
        result['orphan_state'] = bool(networks or volumes or unexpected or (legacy.exists() and any(legacy.iterdir())))
        result['complete'] = True
    except (subprocess.SubprocessError, ValueError):
        result['blocked_reasons'].append('host_inventory_unavailable')
if result['retained_containers']:
    result['blocked_reasons'].append('retained_containers_present')
if result['unknown_containers']:
    result['blocked_reasons'].append('unmanaged_containers_present')
if result['orphan_state']:
    result['blocked_reasons'].append('orphan_state_present')
print(json.dumps(result))
"""
