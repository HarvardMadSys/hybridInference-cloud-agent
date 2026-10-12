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
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from dotenv import dotenv_values

from cloud_agent.compute_secrets import ComputeSecretError
from cloud_agent.ports.external.compute_provider import ProviderError
from cloud_agent.ports.external.host_bootstrap import (
    BootstrapResult,
    HostInventory,
    PreflightResult,
    TransferResult,
)
from cloud_agent.providers.compute.ssh import known_hosts_line

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


# The two remote entrypoints use the same read-only daemon configuration guard.
# Never ask a daemon for this evidence: a socket request can start workloads.
_DAEMON_STORAGE_GUARD = r"""
import json, os, re, shlex, subprocess
from pathlib import Path
def storage_fstab_entries(base, text=None):
    try:
        fstab = Path('/etc/fstab')
        if fstab.is_symlink() or fstab.resolve() != fstab or fstab.exists() and not fstab.is_file():
            return None
        if text is None:
            text = fstab.read_text() if fstab.exists() else ''
        targets = {Path('/var/lib') / name: name for name in ('docker', 'containerd')}
        entries = set()
        for line in text.splitlines():
            fields = [re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), field) for field in line.split('#', 1)[0].split()]
            if len(fields) < 2 or not fields[1].startswith('/'):
                continue
            target = Path('/' + os.path.normpath(fields[1]).lstrip('/'))
            name = targets.get(target)
            if name is None:
                continue
            if name in entries or not 4 <= len(fields) <= 6 or fields[2] != 'none' or any(value != '0' for value in fields[4:]):
                return None
            source = fields[0].rstrip('/')
            if source != os.path.normpath(source) or not source.startswith('/') or Path(source) != base / name:
                return None
            if fields[1].rstrip('/') != '/' + os.path.normpath(fields[1]).lstrip('/'):
                return None
            options = fields[3].split(',')
            if 'bind' not in options or len(set(options)) != len(options):
                return None
            for option in options:
                if option in ('bind', 'defaults', 'rw', 'nofail'):
                    continue
                key, separator, value = option.partition('=')
                if key != 'x-systemd.requires-mounts-for' or not separator or not value.startswith('/') or value.rstrip('/') != os.path.normpath(value) or Path(value.rstrip('/')) != base:
                    return None
            entries.add(name)
        return entries
    except (OSError, ValueError, UnicodeError):
        return None
def storage_mount_units_compatible():
    native = ('/etc/systemd/system', '/etc/systemd/system.control', '/etc/systemd/system.attached', '/run/systemd/system', '/run/systemd/system.control', '/run/systemd/system.attached', '/run/systemd/transient', '/usr/local/lib/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system')
    generated = ('/run/systemd/generator', '/run/systemd/generator.early', '/run/systemd/generator.late')
    try:
        for name in ('docker', 'containerd'):
            unit = 'var-lib-' + name + '.mount'
            # Inspect dormant files too: a later daemon-reload would otherwise
            # activate an operator override absent from current unit metadata.
            for directory in native:
                path = Path(directory) / unit
                if path.exists() or path.is_symlink():
                    return False
            for directory in native + generated:
                path = Path(directory) / (unit + '.d')
                if path.is_symlink() or path.exists() and (not path.is_dir() or any(path.iterdir())):
                    return False
            shown = subprocess.run(['systemctl', 'show', unit, '--property=LoadState', '--property=FragmentPath', '--property=DropInPaths'], capture_output=True, text=True)
            properties = dict(line.split('=', 1) for line in shown.stdout.splitlines() if '=' in line)
            fragment, dropins = properties.get('FragmentPath'), properties.get('DropInPaths')
            if fragment is None or dropins is None or dropins:
                return False
            if properties.get('LoadState') == 'not-found':
                if fragment:
                    return False
            elif shown.returncode or properties.get('LoadState') != 'loaded' or fragment and fragment not in tuple(directory + '/' + unit for directory in generated):
                return False
        return True
    except OSError:
        return False
def deployment_storage_owned(deployment):
    if not isinstance(deployment, dict) or not isinstance(deployment.get('identity'), dict):
        return False
    try:
        root = Path(deployment['root'])
        marker = root / '.compute-identity.json'
        return root.is_dir() and root.resolve() == root and not root.is_symlink() and marker.is_file() and not marker.is_symlink() and marker.resolve() == marker and json.loads(marker.read_text()) == deployment['identity']
    except (KeyError, TypeError, OSError, ValueError):
        return False
def storage_state_compatible(base, owned):
    try:
        if storage_fstab_entries(base) is None or not storage_mount_units_compatible():
            return False
        nonempty = False
        bound = True
        for name in ('docker', 'containerd'):
            source, target = base / name, Path('/var/lib') / name
            if any(path.is_symlink() or path.resolve() != path or path.exists() and not path.is_dir() for path in (source, target)):
                return False
            mounted = target.is_dir() and subprocess.run(['mountpoint', '-q', str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
            if mounted and (not source.is_dir() or not os.path.samefile(source, target)):
                return False
            nonempty = nonempty or any(path.is_dir() and any(path.iterdir()) for path in (source, target))
            bound = bound and mounted
        # A marker alone cannot authorize a newly substituted disk. Both daemon
        # stores must already be bound to this exact managed deployment.
        return not nonempty or owned and bound
    except OSError:
        return False
def _default_path(value, expected):
    return isinstance(value, str) and value.rstrip('/') == expected
def _toml_key_path(value):
    keys = []
    remaining = value.strip()
    while remaining:
        match = re.match(r"(?:([A-Za-z0-9_-]+)|\"([^\"\\]*)\"|'([^']*)')", remaining)
        if not match:
            return None
        keys.append(next(part for part in match.groups() if part is not None))
        remaining = remaining[match.end():].strip()
        if not remaining:
            return tuple(keys)
        if not remaining.startswith('.'):
            return None
        remaining = remaining[1:].strip()
    return None
def _daemon_config_default(path, daemon, explicit=False):
    try:
        config = Path(path)
        if not config.exists():
            return not explicit
        if not config.is_file():
            return False
        text = config.read_text()
        if daemon == 'dockerd':
            value = json.loads(text)
            if not isinstance(value, dict):
                return False
            if any(key in value and not _default_path(value[key], '/var/lib/docker') for key in ('data-root', 'graph')):
                return False
            return 'containerd' not in value or value['containerd'] in ('/run/containerd/containerd.sock', '/var/run/containerd/containerd.sock')
        # Ubuntu 22.04's Python has no tomllib. Inspect literal top-level and
        # grpc keys; imported/indirect config cannot attest the root or socket.
        section = ()
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('['):
                header = re.fullmatch(r'(\[\[?)(.*?)(\]\]?)\s*(?:#.*)?', line)
                if not header or len(header[1]) != len(header[3]):
                    return False
                section = _toml_key_path(header[2])
                if section is None or section[0] == 'grpc' and (section != ('grpc',) or len(header[1]) != 1):
                    return False
                continue
            if section not in ((), ('grpc',)):
                continue
            match = re.fullmatch(r'(.*?)\s*=\s*(.*)', line)
            if not match:
                return False
            key, value = _toml_key_path(match[1]), match[2]
            if key is None:
                return False
            key = section + key
            if key == ('imports',) and value.split('#', 1)[0].strip() != '[]':
                return False
            if key == ('grpc',) or key[0] == 'root' and key != ('root',) or key[:2] == ('grpc', 'address') and len(key) != 2:
                return False
            if key in (('root',), ('grpc', 'address')):
                words = shlex.split(value, comments=True)
                if len(words) != 1 or '\\' in value:
                    return False
                if key == ('root',) and not _default_path(words[0], '/var/lib/containerd'):
                    return False
                if key == ('grpc', 'address') and words[0] not in ('/run/containerd/containerd.sock', '/var/run/containerd/containerd.sock'):
                    return False
        return True
    except (OSError, ValueError, UnicodeError):
        return False
def _daemon_argv_default(argv, daemon):
    if not argv or argv[0] not in ('/usr/bin/' + daemon, '/usr/sbin/' + daemon):
        return False
    if any(any(mark in arg for mark in ('$', '%', '\\', ';', '|', '&')) for arg in argv):
        return False
    expected = '/var/lib/docker' if daemon == 'dockerd' else '/var/lib/containerd'
    config = '/etc/docker/daemon.json' if daemon == 'dockerd' else '/etc/containerd/config.toml'
    roots = ('--data-root', '--graph', '-g') if daemon == 'dockerd' else ('--root', '-r')
    configs = ('--config-file',) if daemon == 'dockerd' else ('--config', '-c')
    addresses = ('--containerd',) if daemon == 'dockerd' else ('--address', '-a')
    explicit = False
    index = 1
    while index < len(argv):
        word = argv[index]
        option, separator, value = word.partition('=')
        if option in roots + configs + addresses:
            if not separator:
                index += 1
                if index >= len(argv):
                    return False
                value = argv[index]
        elif any(word.startswith(short) and len(word) > len(short) for short in roots + configs + addresses if short.startswith('-') and not short.startswith('--')):
            option, value = word[:2], word[2:].removeprefix('=')
        else:
            # Grouped short options can hide a legacy root flag (-Dg/path).
            # Accept only known standalone options outside the root/config set.
            short_options = ('-H', '-D', '-l', '-s', '-p') if daemon == 'dockerd' else ('-l',)
            if word.startswith('-') and not word.startswith('--') and word not in short_options:
                return False
            index += 1
            continue
        if option in roots and not _default_path(value, expected):
            return False
        if option in configs:
            if not value.startswith('/'):
                return False
            config, explicit = value, True
        if option in addresses and value not in ('/run/containerd/containerd.sock', '/var/run/containerd/containerd.sock'):
            return False
        index += 1
    return _daemon_config_default(config, daemon, explicit)
def daemon_storage_default(allow_missing):
    for daemon, service, config in (
        ('dockerd', 'docker', '/etc/docker/daemon.json'),
        ('containerd', 'containerd', '/etc/containerd/config.toml'),
    ):
        if not _daemon_config_default(config, daemon):
            return False
        try:
            shown = subprocess.run(['systemctl', 'show', service, '--property=LoadState', '--property=ExecStart', '--property=MainPID'], capture_output=True, text=True)
        except OSError:
            return False
        properties = dict(line.split('=', 1) for line in shown.stdout.splitlines() if '=' in line)
        if properties.get('LoadState') == 'not-found':
            if not allow_missing or properties.get('MainPID') != '0':
                return False
            continue
        if shown.returncode or properties.get('LoadState') != 'loaded':
            return False
        start = re.fullmatch(r'\{\s*path=([^;]+);\s*argv\[\]=(.*?);\s*[^{}]*\}', properties.get('ExecStart', ''))
        if not start or start[1].strip() not in ('/usr/bin/' + daemon, '/usr/sbin/' + daemon):
            return False
        try:
            if not _daemon_argv_default(shlex.split(start[2].strip()), daemon):
                return False
            pid = properties.get('MainPID', '')
            if not pid.isdigit():
                return False
            if int(pid):
                command = (Path('/proc') / pid / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0')
                if not _daemon_argv_default([part.decode() for part in command], daemon):
                    return False
        except (OSError, ValueError, UnicodeError):
            return False
    return True
"""


_PREFLIGHT = r"""
import hashlib, json, os, platform, shutil, subprocess, sys
from pathlib import Path
params = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
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
if params.get('data_mount'):
    # Root-owned daemon stores need privileged inspection. This helper only
    # reads paths and mount state; preflight never mounts or changes state.
    storage_script = r'''
import json, os, subprocess, sys
from pathlib import Path
def inspect_storage(data_mount, allow_missing=True, deployment=None):
    result = {'data_mount_available': False, 'data_mount_conflict_free': False}
    quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    try:
        base = Path(data_mount)
        if base.is_symlink() or base.resolve() != base or not base.is_dir() or subprocess.run(['mountpoint', '-q', str(base)], **quiet).returncode or os.statvfs(base).f_flag & os.ST_RDONLY:
            return result
        for name in ('docker', 'containerd'):
            for path in (base / name, Path('/var/lib') / name):
                if path.is_dir() and os.statvfs(path).f_flag & os.ST_RDONLY:
                    return result
        result['data_mount_available'] = True
        if not daemon_storage_default(allow_missing):
            return result
        if not storage_state_compatible(base, deployment_storage_owned(deployment)):
            return result
        result['data_mount_conflict_free'] = True
    except OSError:
        pass
    return result
'''
    if os.geteuid() == 0:
        storage = {}
        exec(storage_script, storage)
        checks.update(storage['inspect_storage'](params['data_mount'], params.get('install_prerequisites', True), params.get('deployment')))
    elif root:
        inspected = subprocess.run(
            ['sudo', '-n', 'python3', '-c', storage_script + "\nparams = json.loads(sys.argv[1]); print(json.dumps(inspect_storage(params['data_mount'], params.get('install_prerequisites', True), params.get('deployment'))))", json.dumps(params)],
            capture_output=True, text=True,
        )
        try:
            storage = json.loads(inspected.stdout) if inspected.returncode == 0 else {}
        except ValueError:
            storage = {}
        checks.update({key: storage.get(key) is True for key in ('data_mount_available', 'data_mount_conflict_free')})
    else:
        checks.update({'data_mount_available': False, 'data_mount_conflict_free': False})
machine = Path('/etc/machine-id').read_text().strip().lower()
print(json.dumps({'machine_identity': hashlib.sha256(machine.encode()).hexdigest(),
    'operating_system': osinfo.get('ID', '') + ':' + osinfo.get('VERSION_ID', ''),
    'architecture': platform.machine(), 'checks': checks}))
""".replace(
    "def inspect_storage(data_mount, allow_missing=True, deployment=None):",
    _DAEMON_STORAGE_GUARD
    + "\ndef inspect_storage(data_mount, allow_missing=True, deployment=None):",
)

# Executed with sudo and a tar stream on stdin, never a secret command argument.
# Existing installations require our persisted identity marker. A retry uses
# the same identity and flock; it cannot overwrite or enroll a different Host.
_DEPLOY = (
    _DAEMON_STORAGE_GUARD
    + r"""
import fcntl, io, json, os, subprocess, sys, tarfile
from pathlib import Path
params = json.loads(sys.argv[1])
root = Path(params['root'])
identity = params['identity']
owned_storage = deployment_storage_owned({'root': str(root), 'identity': identity})
def storage_available(base):
    try:
        if base.is_symlink() or base.resolve() != base or not base.is_dir() or subprocess.run(['mountpoint', '-q', str(base)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            return False
        paths = [base] + [path for name in ('docker', 'containerd') for path in (base / name, Path('/var/lib') / name) if path.is_dir()]
        return not any(os.statvfs(path).f_flag & os.ST_RDONLY for path in paths)
    except OSError:
        return False
# Reject an unavailable or read-only disk before even changing the deployment
# checkout. The later check still guards changes between inspection and use.
if params.get('data_mount') and not storage_available(Path(params['data_mount'])):
    print(json.dumps({'ready': False, 'blocked_reasons': ['data_mount_unavailable']}))
    raise SystemExit(0)
if params.get('data_mount') and not daemon_storage_default(params['install']):
    print(json.dumps({'ready': False, 'blocked_reasons': ['data_mount_conflict']}))
    raise SystemExit(0)
if params.get('data_mount') and not storage_state_compatible(Path(params['data_mount']), owned_storage):
    print(json.dumps({'ready': False, 'blocked_reasons': ['data_mount_conflict']}))
    raise SystemExit(0)
root.parent.mkdir(parents=True, exist_ok=True)
if root.is_symlink() or root.resolve() != root:
    raise SystemExit('unsafe deployment root')
lock_path = root.parent / ('.' + root.name + '.compute-lock')
if lock_path.is_symlink():
    raise SystemExit('unsafe lock')
with open(lock_path, 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    marker = root / '.compute-identity.json'
    # Capture ownership before this invocation can create its own marker.
    owned_storage = deployment_storage_owned({'root': str(root), 'identity': identity})
    if params.get('data_mount') and not storage_state_compatible(Path(params['data_mount']), owned_storage):
        print(json.dumps({'ready': False, 'blocked_reasons': ['data_mount_conflict']}))
        raise SystemExit(0)
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
    environment.update({'DEBIAN_FRONTEND': 'noninteractive', 'AGENT_DEPLOYMENT_SHA': params['sha'], 'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': 'default'})
    for name in ('DOCKER_TLS', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH'):
        environment.pop(name, None)
    quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
    def blocked(step):
        print(json.dumps({'ready': False, 'blocked_reasons': [step]}))
        raise SystemExit(0)
    def run(step, args):
        if subprocess.run(args, cwd=root, env=environment, **quiet).returncode:
            blocked(step)
    if params.get('data_mount'):
        # Keep Docker's and containerd's default paths, but on the provider's
        # large local disk: a Host's whole state is what moves between nodes.
        base = Path(params['data_mount'])
        if not storage_available(base):
            blocked('data_mount_unavailable')
        if not daemon_storage_default(params['install']):
            blocked('data_mount_conflict')
        if not storage_state_compatible(base, owned_storage):
            blocked('data_mount_conflict')
        fstab = Path('/etc/fstab')
        stores = []
        for name in ('docker', 'containerd'):
            source, target = base / name, Path('/var/lib') / name
            if source.is_symlink() or source.resolve() != source or target.is_symlink() or target.resolve() != target:
                blocked('data_mount_conflict')
            if (source.exists() and not source.is_dir()) or (target.exists() and not target.is_dir()):
                blocked('data_mount_conflict')
            source.mkdir(mode=0o711, exist_ok=True)
            target.mkdir(mode=0o711, exist_ok=True)
            mounted = subprocess.run(['mountpoint', '-q', str(target)], **quiet).returncode == 0
            if mounted and not os.path.samefile(source, target):
                # An already mounted daemon must use this Host's selected disk.
                # Never accept or replace a different store on a retry.
                blocked('data_mount_conflict')
            if not mounted and any(target.iterdir()):
                # Neither store may be hidden or reclaimed from an unrelated
                # daemon's inventory. Retained files are never discarded here.
                blocked('data_mount_conflict')
            stores.append((name, source, target, mounted))
        unmounted = [target for _, _, target, mounted in stores if not mounted]
        active_daemons = [name for name in ('docker.socket', 'docker', 'containerd') if subprocess.run(['systemctl', 'is-active', '--quiet', name], **quiet).returncode == 0] if unmounted else []
        if unmounted and active_daemons:
            if subprocess.run(['systemctl', 'stop', *active_daemons], cwd=root, env=environment, **quiet).returncode:
                # systemctl may stop some requested units before returning an
                # error. Restore only the units that were active on entry.
                run('data_mount_failed', ['systemctl', 'start', *active_daemons])
                blocked('data_mount_failed')
        # Recheck after stopping: a daemon may have populated an initially
        # empty store while its disk was being inspected.
        if not storage_state_compatible(base, owned_storage):
            if active_daemons:
                run('data_mount_failed', ['systemctl', 'start', *active_daemons])
            blocked('data_mount_conflict')
        for name, source, target, mounted in stores:
            if not mounted:
                run('data_mount_failed', ['mount', '--bind', str(source), str(target)])
        # Re-read operator configuration after mounting and immediately before
        # persistence. Equivalent existing entries are reused without rewriting.
        current = fstab.read_text() if fstab.exists() else ''
        existing = storage_fstab_entries(base, current)
        if existing is None or not storage_mount_units_compatible():
            if active_daemons:
                run('data_mount_failed', ['systemctl', 'start', *active_daemons])
            blocked('data_mount_conflict')
        missing = [f'{source} {target} none bind,nofail,x-systemd.requires-mounts-for={base} 0 0' for name, source, target, _ in stores if name not in existing]
        if missing:
            with fstab.open('a') as persisted:
                if current and not current.endswith('\n'):
                    persisted.write('\n')
                persisted.write('\n'.join(missing) + '\n')
        for name, _, target, _ in stores:
            # On reboot a missing disk must keep its daemon stopped rather
            # than silently opening an empty store under the bare target.
            dropin = Path('/etc/systemd/system') / (name + '.service.d')
            dropin.mkdir(parents=True, exist_ok=True)
            (dropin / 'cloud-agent-storage.conf').write_text(f'[Unit]\nRequiresMountsFor={base} {target}\n')
        run('data_mount_failed', ['systemctl', 'daemon-reload'])
    if params['install']:
        run('prerequisites_update_failed', ['apt-get', 'update'])
        run('prerequisites_install_failed', ['apt-get', 'install', '-y', 'docker.io', 'docker-compose-v2', 'docker-buildx', 'curl', 'ca-certificates', 'xz-utils', 'zstd', 'rsync', 'nftables'])
        run('docker_start_failed', ['systemctl', 'enable', '--now', 'docker'])
        # The bundled script validates its pinned download checksum and KVM.
        run('kata_install_failed', ['bash', 'deploy/host/setup_kata_runtime.sh'])
    if not params.get('activate', True):
        # Staged to take a Host over: the recipe is in place, nothing runs or enrolls.
        print(json.dumps({'ready': True, 'blocked_reasons': []}))
        raise SystemExit(0)
    # A node that received another node's state starts with its daemons stopped.
    run('docker_start_failed', ['systemctl', 'start', 'containerd', 'docker'])
    if params.get('reenroll'):
        # The Host name was handed back to this node and enrolled elsewhere in
        # between: its persisted credential is revoked, so spend the new token.
        credential = Path('/var/lib/cloud-agent') / identity['host'] / 'credential'
        if credential.is_symlink():
            blocked('host_deployment_failed')
        credential.unlink(missing_ok=True)
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
)

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
                "ssh_configuration", "This node has no SSH connection configuration"
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
        """Verify identity, prerequisites and selected storage without changing state."""
        return await self._preflight(node, profile)

    async def _preflight(
        self,
        node: NodeObservation,
        profile: DeploymentProfile,
        *,
        deployment: dict[str, Any] | None = None,
    ) -> PreflightResult:
        """Only an existing allocation may attest ownership of retained storage."""
        data_mount = profile.sandbox_storage_path or profile.data_mount
        parameters: dict[str, Any] = {
            "data_mount": data_mount,
            "install_prerequisites": profile.install_prerequisites,
        }
        if deployment is not None:
            parameters["deployment"] = deployment
        text = await self._transport.run(
            self._connection(node),
            "python3 -c " + shlex.quote(_PREFLIGHT) + " " + shlex.quote(json.dumps(parameters)),
        )
        result = self._json(text)
        checks = result.get("checks", {})
        required = {"supported_os", "architecture", "privileged_deploy", "kvm"}
        if not profile.install_prerequisites:
            required |= {"docker", "compose"}
        blocked = [name for name in sorted(required) if checks.get(name) is not True]
        if data_mount:
            if checks.get("data_mount_available") is not True:
                blocked.append("data_mount_unavailable")
            elif checks.get("data_mount_conflict_free") is not True:
                blocked.append("data_mount_conflict")
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
    def _params(
        profile: DeploymentProfile,
        intent: BootstrapIntent,
        *,
        install: bool | None = None,
        activate: bool = True,
        reenroll: bool = False,
    ) -> dict[str, Any]:
        identity = {
            "allocation_id": intent.allocation_id,
            "node_id": intent.node_id,
            "host": intent.host,
            "deployment_sha": profile.deployment_sha,
        }
        if intent.namespace:
            # Only a node that took a Host over records the namespace it
            # inherited, so markers written before rotation existed still match.
            identity["namespace"] = intent.namespace
        if profile.sandbox_storage_path is not None:
            identity["sandbox_storage_path"] = profile.sandbox_storage_path
        return {
            "root": profile.deploy_root,
            "sha": profile.deployment_sha,
            "install": profile.install_prerequisites if install is None else install,
            "activate": activate,
            "reenroll": reenroll,
            "data_mount": profile.sandbox_storage_path or profile.data_mount,
            "project": intent.project,
            "identity": identity,
        }

    async def _archive(
        self,
        profile: DeploymentProfile,
        intent: BootstrapIntent,
        enrollment_token: SecretStr | None,
    ) -> bytes:
        try:
            if profile.source_bundle.startswith("stored-profile:"):
                source = await self._resolver.bundle(profile.source_bundle)
            else:
                source = Path(profile.source_bundle).read_bytes()
        except (OSError, ComputeSecretError):
            raise ProviderError(
                "deployment_profile_invalid", "Trusted deployment bundle is unavailable"
            ) from None
        if hashlib.sha256(source).hexdigest() != profile.source_sha256:
            raise ProviderError(
                "deployment_profile_invalid",
                "Trusted deployment bundle digest differs from its profile",
            )
        approved = ("deployment:", "stored-profile:")
        if not profile.environment_ref.startswith(approved) or any(
            not ref.startswith(approved) for ref in profile.files.values()
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
                "COMPOSE_PROJECT": intent.project,
                "AGENT_RUNNER_HOST": intent.host,
                "AGENT_HOST_STATE_ROOT": "/var/lib/cloud-agent/" + intent.host,
                "AGENT_DEPLOYMENT_SHA": profile.deployment_sha,
                "AGENT_SANDBOX_BACKEND": "kata",
            }
        )
        if profile.sandbox_storage_path is not None:
            # Both daemon stores are bound from the selected disk to their
            # standard paths. Broker and runner must attest that same daemon.
            values["AGENT_RETAINED_DOCKER_DATA_ROOT"] = "/var/lib/docker"
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
        *,
        prerequisites: bool | None = None,
        reenroll: bool = False,
    ) -> BootstrapResult:
        """Install the trusted host recipe while retaining its existing safety gates.

        ``prerequisites=False`` skips package and Kata installation on a node
        staged earlier, which keeps a Host takeover's pause short.
        ``reenroll`` discards the node's persisted Host credential so the
        supplied token enrolls it again.
        """
        if reenroll and enrollment_token is None:
            raise ProviderError("invalid_configuration", "Re-enrollment needs a fresh token")
        return await self._deploy(
            node,
            profile,
            self._params(profile, intent, install=prerequisites, activate=True, reenroll=reenroll),
            intent,
            enrollment_token,
        )

    async def stage(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> BootstrapResult:
        """Install prerequisites and the recipe without starting or enrolling a Host."""
        return await self._deploy(
            node, profile, self._params(profile, intent, activate=False), intent, None
        )

    async def _deploy(
        self,
        node: NodeObservation,
        profile: DeploymentProfile,
        params: dict[str, Any],
        intent: BootstrapIntent,
        enrollment_token: SecretStr | None,
    ) -> BootstrapResult:
        preflight = await self._preflight(
            node, profile, deployment={"root": params["root"], "identity": params["identity"]}
        )
        if not preflight.ready:
            return BootstrapResult(
                ready=False,
                host=intent.host,
                deployment_sha=profile.deployment_sha,
                blocked_reasons=preflight.blocked_reasons,
            )
        archive = await self._archive(profile, intent, enrollment_token)
        command = (
            "sudo -n python3 -c " + shlex.quote(_DEPLOY) + " " + shlex.quote(json.dumps(params))
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

    async def stop(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> None:
        """Stop the Host stack, its sandboxes and the container daemons, keeping all state."""
        params = {**self._params(profile, intent), "broker_drain_seconds": _BROKER_DRAIN_SECONDS}
        script = "sudo -n python3 -c " + shlex.quote(_STOP) + " " + shlex.quote(json.dumps(params))
        result = self._json(
            await self._transport.run(self._connection(node), script, timeout=_STOP_TIMEOUT_SECONDS)
        )
        if result.get("stopped") is not True:
            raise ProviderError(
                str(result.get("reason") or "host_stop_failed"),
                "The Host could not be stopped",
                retryable=result.get("reason") == "host_stop_failed",
            )

    async def transfer(
        self,
        source: NodeObservation,
        target: NodeObservation,
        target_profile: DeploymentProfile,
        target_intent: BootstrapIntent,
        *,
        final: bool,
        first: bool = False,
    ) -> TransferResult:
        """Copy Docker's and containerd's state directly from source to target.

        The control-plane SSH channel cannot carry this much data, so the source
        pushes over its own SSH session with a key made for this transfer only
        and checks the target against the host key this control plane pinned.
        ``first`` empties the staged target's fresh daemons before the copy;
        ``final`` runs on a stopped source and deletes what it no longer has.
        """
        source_connection, target_connection = self._connection(source), self._connection(target)
        if target_connection.host_key:
            target_known = known_hosts_line(
                target_connection.address, target_connection.port, target_connection.host_key
            )
        elif target_connection.known_hosts_ref:
            target_known = await self._resolver.resolve(target_connection.known_hosts_ref)
        else:
            raise ProviderError("ssh_configuration", "The target host key is not verified")
        public_key = (
            await self._transport.run(
                source_connection, "sudo -n sh -c " + shlex.quote(_TRANSFER_KEY)
            )
        ).strip()
        if not re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2} ca-transfer", public_key):
            raise ProviderError("invalid_response", "The source returned an invalid transfer key")
        target_params = self._params(target_profile, target_intent)
        authorize = {
            "key": public_key,
            "first": first,
            "root": target_params["root"],
            "identity": target_params["identity"],
        }
        authorized = self._json(
            await self._transport.run(
                target_connection,
                "python3 -c "
                + shlex.quote(_AUTHORIZE_TRANSFER)
                + " "
                + shlex.quote(json.dumps(authorize)),
                timeout=900,
            )
        )
        if authorized.get("authorized") is not True:
            raise ProviderError(
                str(authorized.get("reason") or "transfer_failed"),
                "The target node cannot receive the Host's state",
            )
        push = {
            "address": target_connection.address,
            "port": target_connection.port,
            "user": target_connection.username,
            "final": final,
            "streams": _TRANSFER_STREAMS,
        }
        started = time.monotonic()
        result = self._json(
            await self._transport.run(
                source_connection,
                "sudo -n python3 -c " + shlex.quote(_PUSH) + " " + shlex.quote(json.dumps(push)),
                stdin=target_known.encode(),
                timeout=_TRANSFER_TIMEOUT_SECONDS,
            )
        )
        if result.get("copied") is not True:
            raise ProviderError(
                "transfer_failed", "Copying the Host's state did not finish", retryable=True
            )
        return TransferResult(
            bytes_transferred=int(result.get("bytes", 0)),
            seconds=round(time.monotonic() - started, 1),
            streams=int(result.get("streams", 1)),
        )

    async def revoke_transfer(self, source: NodeObservation, target: NodeObservation) -> None:
        """Remove the one-off transfer key from both machines."""
        await self._transport.run(self._connection(target), _REVOKE_TRANSFER_TARGET)
        await self._transport.run(
            self._connection(source), "sudo -n rm -rf /var/lib/cloud-agent-transfer"
        )

    async def wipe(
        self, node: NodeObservation, profile: DeploymentProfile, intent: BootstrapIntent
    ) -> None:
        """Delete a node's container state after its successor took the Host over."""
        params = self._params(profile, intent)
        script = "sudo -n python3 -c " + shlex.quote(_WIPE) + " " + shlex.quote(json.dumps(params))
        result = self._json(await self._transport.run(self._connection(node), script, timeout=1800))
        if result.get("wiped") is not True:
            reason = result.get("reason")
            raise ProviderError(
                str(reason or "wipe_failed"),
                "The node's container state could not be removed",
                retryable=reason is None,
            )


# Parallel rsync processes for one copy. A single SSH stream between CloudLab
# sites carries about 30 MB/s; four reached 128 MB/s in the 2026-10-10 drill.
_TRANSFER_STREAMS = 6
_TRANSFER_TIMEOUT_SECONDS = 4 * 3600

# The broker's drain must end early enough inside the stop's SSH deadline to
# leave time for the runner, the remaining containers and the daemons.
_STOP_TIMEOUT_SECONDS = 900
_BROKER_DRAIN_SECONDS = 600

_STOP = r"""
import json, subprocess, sys
from pathlib import Path
params = json.loads(sys.argv[1])
marker = Path(params['root']) / '.compute-identity.json'
if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != params['identity']:
    print(json.dumps({'stopped': False, 'reason': 'deployment_identity_conflict'}))
    raise SystemExit(0)
quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
def running(*filters):
    result = subprocess.run(['docker', 'ps', '-q', *filters], capture_output=True, text=True)
    return result.stdout.split() if result.returncode == 0 else []
def service(name):
    return running('--filter', 'label=com.docker.compose.project=' + params['project'],
                   '--filter', 'label=com.docker.compose.service=' + name)
if subprocess.run(['systemctl', 'is-active', '--quiet', 'docker'], **quiet).returncode == 0:
    # The runner first: nothing may claim or resume while the state is moving.
    runner = service('runner')
    if runner:
        subprocess.run(['docker', 'stop', '-t', '60', *runner], **quiet)
    # Then the broker, alone: its shutdown stops each running sandbox cleanly.
    # While lifecycle archives are enabled, the next node's broker quarantines
    # a sandbox that Docker stopped behind the broker's back.
    broker = service('workspace-broker')
    if broker:
        drain = str(params['broker_drain_seconds'])
        subprocess.run(['docker', 'stop', '-t', drain, *broker], **quiet)
    # Explicitly stopped containers stay stopped when a daemon starts again,
    # including on the node that receives this state. The CLI stops them in
    # parallel, each with its own configured grace period.
    others = running()
    if others:
        subprocess.run(['docker', 'stop', *others], **quiet)
subprocess.run(['systemctl', 'stop', 'docker.socket', 'docker', 'containerd'], **quiet)
active = any(subprocess.run(['systemctl', 'is-active', '--quiet', unit], **quiet).returncode == 0
             for unit in ('docker', 'containerd'))
# Match the process name, not the command line: this script's own text
# names the shim too. Kata and runc shims share the truncated name.
shims = subprocess.run(['pgrep', '-x', 'containerd-shim'], **quiet).returncode == 0
ok = not active and not shims
print(json.dumps({'stopped': ok, 'reason': None if ok else 'host_stop_failed'}))
"""

_TRANSFER_KEY = (
    "umask 077; d=/var/lib/cloud-agent-transfer; mkdir -p $d; "
    "[ -f $d/key ] || ssh-keygen -q -t ed25519 -N '' -C ca-transfer -f $d/key; cat $d/key.pub"
)

# Runs as the management user on the target; sudo only for daemon and state steps.
_AUTHORIZE_TRANSFER = r"""
import json, subprocess, sys
from pathlib import Path
params = json.loads(sys.argv[1])
def refuse(reason):
    print(json.dumps({'authorized': False, 'reason': reason}))
    raise SystemExit(0)
marker = subprocess.run(['sudo', '-n', 'cat', params['root'] + '/.compute-identity.json'], capture_output=True, text=True)
try:
    if marker.returncode or json.loads(marker.stdout) != params['identity']:
        refuse('deployment_identity_conflict')
except ValueError:
    refuse('deployment_identity_conflict')
quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
if subprocess.run(['sudo', '-n', 'systemctl', 'stop', 'docker.socket', 'docker', 'containerd'], **quiet).returncode:
    refuse('transfer_failed')
if params['first']:
    # A staged node holds no Host state yet. Empty its fresh daemons so the
    # copy becomes an exact replica rather than a merge.
    for path in ('/var/lib/docker', '/var/lib/containerd'):
        if subprocess.run(['sudo', '-n', 'find', path, '-xdev', '-mindepth', '1', '-delete'], **quiet).returncode:
            refuse('transfer_failed')
ssh = Path.home() / '.ssh'
ssh.mkdir(mode=0o700, exist_ok=True)
keys = ssh / 'authorized_keys'
current = keys.read_text() if keys.exists() else ''
entry = 'restrict ' + params['key']
if entry not in current.splitlines():
    keys.write_text(current.rstrip('\n') + ('\n' if current.strip() else '') + entry + '\n')
keys.chmod(0o600)
print(json.dumps({'authorized': True}))
"""

_REVOKE_TRANSFER_TARGET = (
    'f=~/.ssh/authorized_keys; [ -f "$f" ] || exit 0; '
    '{ grep -v \' ca-transfer$\' "$f" || true; } > "$f.ca"; '
    'mv "$f.ca" "$f"; chmod 600 "$f"'
)

# Runs as root on the source. The large directories of many independent
# entries (snapshots and content blobs) are spread over parallel streams; one
# more stream carries everything else. -x keeps each stream on the data disk:
# running containers' merged root file systems are mounted under
# /var/lib/docker/rootfs and must never be copied as files.
_PUSH = r"""
import json, os, re, subprocess, sys, tempfile, threading
from pathlib import Path
params = json.loads(sys.argv[1])
work = Path('/var/lib/cloud-agent-transfer')
known = work / 'known_hosts'
known.write_bytes(sys.stdin.buffer.read())
known.chmod(0o600)
ssh = ' '.join([
    'ssh', '-F', '/dev/null', '-i', str(work / 'key'), '-p', str(int(params['port'])),
    '-o', 'BatchMode=yes', '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
    '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(known),
    '-o', 'GlobalKnownHostsFile=/dev/null', '-o', 'UpdateHostKeys=no',
    '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=8',
])
final = bool(params['final'])
destination = params['user'] + '@' + params['address'] + ':'
common = ['-HAXSx', '--numeric-ids', '--stats', '-e', ssh, '--rsync-path=sudo -n rsync']
if final:
    common.append('--delete')
large = ['/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots',
         '/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256']
jobs = []
if final:
    # Remove entries the source no longer has; the streams below only sync
    # the entries that still exist. -d updates this one level without recursing.
    for path in large:
        if os.path.isdir(path):
            jobs.append(['rsync', '-dlptgoD', *common, path + '/', destination + path + '/'])
cleanup, jobs = jobs, []
jobs.append(['rsync', '-a', *common,
             *[part for path in large for part in ('--exclude', path.removeprefix('/var/lib') + '/*')],
             '/var/lib/docker', '/var/lib/containerd', destination + '/var/lib/'])
lists = []
for path in large:
    if not os.path.isdir(path):
        continue
    entries = sorted(os.listdir(path))
    count = max(1, min(int(params['streams']), len(entries)))
    for index in range(count):
        chunk = entries[index::count]
        if not chunk:
            continue
        listing = tempfile.NamedTemporaryFile('w', dir=work, delete=False)
        listing.write('\n'.join(chunk) + '\n')
        listing.close()
        lists.append(listing.name)
        jobs.append(['rsync', '-a', '-r', *common, '--files-from=' + listing.name,
                     path + '/', destination + path + '/'])
results = []
lock = threading.Lock()
def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    with lock:
        results.append(result)
for command in cleanup:
    run(command)
pending = list(jobs)
def worker():
    while True:
        with lock:
            if not pending:
                return
            command = pending.pop(0)
        run(command)
threads = [threading.Thread(target=worker) for _ in range(max(1, int(params['streams'])))]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
for name in lists:
    os.unlink(name)
# 24: files vanished while a running source was copied. Only a final copy,
# taken from a stopped source, must be complete.
allowed = {0} if final else {0, 24}
copied = all(result.returncode in allowed for result in results)
total = 0
for result in results:
    match = re.search(r'Total transferred file size: ([0-9,]+)', result.stdout)
    if match:
        total += int(match.group(1).replace(',', ''))
print(json.dumps({'copied': copied, 'bytes': total, 'streams': len(threads)}))
"""

_WIPE = r"""
import json, shutil, subprocess, sys
from pathlib import Path
params = json.loads(sys.argv[1])
marker = Path(params['root']) / '.compute-identity.json'
if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != params['identity']:
    print(json.dumps({'wiped': False, 'reason': 'deployment_identity_conflict'}))
    raise SystemExit(0)
quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
subprocess.run(['systemctl', 'stop', 'docker.socket', 'docker', 'containerd'], **quiet)
removed = all(
    subprocess.run(['find', path, '-xdev', '-mindepth', '1', '-delete'], **quiet).returncode == 0
    for path in ('/var/lib/docker', '/var/lib/containerd')
    if Path(path).is_dir()
)
state = Path('/var/lib/cloud-agent') / params['identity']['host']
if state.exists() and not state.is_symlink():
    shutil.rmtree(state)
# Release reads an empty, running daemon as proof that nothing is retained.
started = subprocess.run(['systemctl', 'start', 'containerd', 'docker'], **quiet).returncode == 0
print(json.dumps({'wiped': removed and started}))
"""


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
        project = params.get('project') or 'ca-' + params['identity']['node_id'].replace('-', '').lower()
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
