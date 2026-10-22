"""The scripts that copy a Host's Docker and containerd state from node to node.

:class:`~cloud_agent.providers.compute.host_bootstrap.SshHostBootstrap` sends
them over SSH and reads their answers. The source makes a one-off key
(``_TRANSFER_KEY``); the target accepts it for rsync's receiving server only
(``_AUTHORIZE_TRANSFER``, ``_TRANSFER_RECEIVE``); the source pushes
(``_PUSH``); the target drops the key afterwards (``_REVOKE_TRANSFER_TARGET``).
Two real nodes run them in ``tests/external/test_host_move_out_two_nodes.py``.
"""

from __future__ import annotations

_TRANSFER_KEY = (
    "umask 077; d=/var/lib/cloud-agent-transfer; mkdir -p $d; "
    "[ -f $d/key ] || ssh-keygen -q -t ed25519 -N '' -C ca-transfer -f $d/key; cat $d/key.pub"
)

# The forced command of the transfer key on the target. The copy in _PUSH sends
# only rsync's receiving server, through sudo, into the directories it copies;
# anything else — a shell, another program, rsync --sender reading the target's
# files back, another destination — is refused before it runs. Receiving into
# /var/lib as root still trusts the source with the target's container state,
# which is what the copy is for, but no longer with an interactive root shell.
# Server arguments are what rsync 3.2.7 (Ubuntu 22.04 and 24.04) sends for the
# _PUSH options: one short-option cluster ending in its capability string, a
# few long options, then "." and the destination.
_TRANSFER_RECEIVE = r"""
import os, re, shlex, sys
destinations = {
    '/var/lib/',
    '/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/',
    '/var/lib/containerd/io.containerd.content.v1.content/blobs/sha256/',
}
options = {'--stats', '--numeric-ids', '--delete', '--no-r'}
try:
    argv = shlex.split(os.environ.get('SSH_ORIGINAL_COMMAND', ''))
except ValueError:
    argv = []
if not (
    len(argv) >= 7
    and argv[:4] == ['sudo', '-n', 'rsync', '--server']
    and argv[-2] == '.'
    and argv[-1] in destinations
    and all(
        re.fullmatch(r'-[ldHogDtpAXrRxS]+(?:e\.[A-Za-z]+)?', flag) or flag in options
        for flag in argv[4:-2]
    )
):
    sys.stderr.write('this key only receives the Host state transfer\n')
    raise SystemExit(1)
os.execvp('sudo', argv)
"""

# Runs as the management user on the target; sudo only for daemon and state steps.
_AUTHORIZE_TRANSFER = r"""
import ipaddress, json, re, socket, subprocess, sys
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
# Checked before anything here changes. The key works only from the source, as
# this machine resolves the address the control plane reaches it at; sshd
# matches from= against the peer's address.
try:
    sources = sorted({
        str(ipaddress.ip_address(info[4][0].split('%')[0]))
        for info in socket.getaddrinfo(params['source'], None, type=socket.SOCK_STREAM)
    })
except (OSError, UnicodeError, ValueError):
    sources = []
if not sources:
    refuse('transfer_failed')
receive = Path.home() / '.ssh' / 'ca-transfer-receive'
if not all(re.fullmatch(r'[A-Za-z0-9_./+-]+', path) for path in (sys.executable, str(receive))):
    refuse('transfer_failed')
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
receive.unlink(missing_ok=True)
receive.write_text(RECEIVE_PROGRAM)
receive.chmod(0o700)
keys = ssh / 'authorized_keys'
current = keys.read_text() if keys.exists() else ''
entry = (
    'restrict,from="' + ','.join(sources) + '",command="' + sys.executable + ' -I '
    + str(receive) + '" ' + params['key']
)
# One transfer key at a time: an entry from an earlier attempt, or from a
# control plane that authorized it without these restrictions, is replaced.
kept = [line for line in current.splitlines() if not line.endswith(' ca-transfer')]
keys.write_text(''.join(line + '\n' for line in [*kept, entry]))
keys.chmod(0o600)
print(json.dumps({'authorized': True}))
""".replace("RECEIVE_PROGRAM", repr(_TRANSFER_RECEIVE))

_REVOKE_TRANSFER_TARGET = (
    "rm -f ~/.ssh/ca-transfer-receive; "
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
# The first job, alone: it creates the directories the parallel streams
# write into. On an emptied target they do not exist yet, and a stream that
# started first failed to create its destination (rsync makes only the last
# path component). The job leaves out the large directories' contents, so
# it is short.
run(jobs.pop(0))
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
