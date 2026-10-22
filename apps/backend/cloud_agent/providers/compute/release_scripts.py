"""The two scripts a node runs before its resources are released.

Both run as root through :class:`~cloud_agent.providers.compute.host_bootstrap.SshHostBootstrap`,
which owns the SSH command line and the answers. ``_QUIESCE`` stops the
releasing node's runner and removes the self-test sandboxes it left;
``_INVENTORY`` then reads everything Docker and the Host's state still hold.
Real Docker runs both in ``tests/external/test_host_release_real_docker.py``.
"""

from __future__ import annotations

# A runner that stops when its credential is refused would, under its restart
# policy, start again and qualify the machine on a fresh self-test sandbox each
# time; the inventory would count one that is running. `docker stop` marks the
# runner stopped on purpose, so nothing starts it again short of a redeploy.
# The sandbox manager keeps running: the release still removes through it.
# A self-test sandbox carries its own label in this deployment's namespace and
# runs no one's chat; one its runner was stopped in the middle of is removed.
_QUIESCE = r"""
import json, shutil, subprocess, sys
from pathlib import Path
params = json.loads(sys.argv[1])
root = Path(params['root'])
marker = root / '.compute-identity.json'
def answer(**fields):
    print(json.dumps(fields))
    raise SystemExit(0)
if root.exists() and any(root.iterdir()) and (not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != params['identity']):
    answer(quiesced=False, reason='deployment_identity_conflict')
if not shutil.which('docker'):
    answer(quiesced=True, probes_removed=0)
project = params.get('project') or 'ca-' + params['identity']['node_id'].replace('-', '').lower()
quiet = {'stdout': subprocess.DEVNULL, 'stderr': subprocess.DEVNULL}
def listed(*filters):
    return subprocess.check_output(['docker', 'ps', '-q', '--no-trunc', *filters], stderr=subprocess.DEVNULL, text=True).split()
runner = ('--filter', 'label=com.docker.compose.project=' + project, '--filter', 'label=com.docker.compose.service=runner')
probe = (
    '-a', '--filter', 'label=org.freeinference.chat-probe-created',
    '--filter', 'label=org.freeinference.chat-sandbox=true',
    '--filter', 'label=org.freeinference.sandbox-namespace=' + project,
    '--filter', 'label=org.freeinference.chat-runtime=preflight',
)
try:
    running = listed(*runner)
    if running:
        subprocess.check_call(['docker', 'stop', '-t', '60', *running], **quiet)
    if listed(*runner):
        answer(quiesced=False, reason='host_quiesce_failed')
    probes = listed(*probe)
    if probes:
        subprocess.check_call(['docker', 'rm', '--force', *probes], **quiet)
except (subprocess.SubprocessError, OSError):
    answer(quiesced=False, reason='host_quiesce_failed')
answer(quiesced=True, probes_removed=len(probes))
"""

_INVENTORY = r"""
import hashlib, json, shutil, subprocess, sys
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
        # One listing, labels and all: a container removed between a list and
        # a separate inspect of it would fail the whole read.
        listing = output(['docker', 'ps', '-a', '--no-trunc', '--format',
                          '{"sandbox":{{json (.Label "org.freeinference.chat-sandbox")}},'
                          '"project":{{json (.Label "com.docker.compose.project")}}}'])
        project = params.get('project') or 'ca-' + params['identity']['node_id'].replace('-', '').lower()
        for line in listing.splitlines():
            labels = json.loads(line)
            if labels['sandbox'] == 'true':
                result['retained_containers'] += 1
            elif labels['project'] != project:
                result['unknown_containers'] += 1
        networks = output(['docker', 'network', 'ls', '-q', '--filter', 'label=org.freeinference.chat-sandbox-network=true'])
        volumes = output(['docker', 'volume', 'ls', '-q'])
        state_root = Path('/var/lib/cloud-agent')
        state = state_root / params['identity']['host']
        # Only this exact deployment's enrollment credential and flock file
        # are inert. Another host, nested same-named file, or a symlink can
        # hide retained state and must never be excused by its basename.
        inert = {state / 'credential', state / '.enroll.lock'}
        # Besides them, only receipts the control plane recorded exactly: the
        # latest checkpoint of each sandbox (its data is in object storage),
        # and each confirmed removal of a container a moved group left behind.
        # Anything else in the archive state, finished or not, still counts.
        archives = state / 'sandbox-archives'
        recorded = json.loads(sys.stdin.read() or '{}')
        def digest(value):
            return hashlib.sha256(str(value).encode()).hexdigest()
        checkpoints = {digest(sandbox): expected for sandbox, expected in (recorded.get('checkpoints') or {}).items()}
        removals = {digest(request): dict(expected, request_id=request) for request, expected in (recorded.get('removals') or {}).items()}
        def receipt(path):
            try:
                value = json.loads(path.read_text())
            except (OSError, ValueError):
                return None
            return value if isinstance(value, dict) else None
        def removed(directory, expected):
            # The two receipts the Sandbox Manager's discard leaves, exactly as
            # it writes them for this request: the container it removed, or one
            # it proved already absent.
            if directory.is_symlink() or sorted(p.name for p in directory.iterdir()) != ['discard.json', 'discarded.json']:
                return False
            started, finished = receipt(directory / 'discard.json'), receipt(directory / 'discarded.json')
            identity = {
                'thread_id': expected['thread_id'], 'request_id': expected['request_id'],
                'generation': expected['generation'], 'container_id': expected['container_id'],
                'archive_key': '',
            }
            if started is None or started.get('identity') != identity:
                return False
            if started.get('target_absent') is True:
                return (
                    started == {'identity': identity, 'sandbox_id': expected['sandbox_id'], 'target_absent': True}
                    and finished == {'identity': identity, 'sandbox_id': expected['sandbox_id']}
                )
            spec = started.get('spec')
            return (
                set(started) == {'identity', 'spec'} and isinstance(spec, dict)
                and spec.get('sandbox_id') == expected['sandbox_id']
                and spec.get('generation') == expected['generation']
                and finished == {'identity': identity}
            )
        def accounted(path):
            if path.parent == archives / 'checkpoints' and path.suffix == '.json':
                # The latest checkpoint the control plane holds for that
                # sandbox from this Host, every recorded field the same.
                expected, value = checkpoints.get(path.stem), receipt(path)
                return (
                    expected is not None and value is not None
                    and all(key in value and value[key] == expected[key] for key in expected)
                )
            if path.parent.parent == archives and path.parent.name in removals:
                return removed(path.parent, removals[path.parent.name])
            return False
        paths = list(state_root.rglob('*')) if state_root.exists() else []
        unexpected = state_root.is_symlink() or any(p.is_symlink() or (not p.is_dir() and p not in inert and not accounted(p)) for p in paths)
        legacy = Path('/var/lib/hybridinference')
        result['orphan_state'] = bool(networks or volumes or unexpected or (legacy.exists() and any(legacy.iterdir())))
        result['complete'] = True
    except (subprocess.SubprocessError, ValueError, KeyError, TypeError):
        result['blocked_reasons'].append('host_inventory_unavailable')
if result['retained_containers']:
    result['blocked_reasons'].append('retained_containers_present')
if result['unknown_containers']:
    result['blocked_reasons'].append('unmanaged_containers_present')
if result['orphan_state']:
    result['blocked_reasons'].append('orphan_state_present')
print(json.dumps(result))
"""
