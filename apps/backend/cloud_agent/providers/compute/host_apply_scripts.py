"""The one remote script that changes a running managed Host's configuration.

Runs through the existing SSH executor. It takes everything on stdin, so a
credential never reaches a command line, and it refuses any key, file or
service outside these short lists: a rotation can replace the sandbox manager
credential and the tunnel keys, nothing else. It backs up what it replaces,
drains the runner and then the sandbox manager with no forced-kill deadline
(as ``sandbox_host.sh`` does) only when those services are named, recreates
just the named services, and puts everything back if they do not stay up.
"""

from __future__ import annotations

#: The only ``.env`` keys the script may set or remove.
APPLY_KEYS = frozenset({"AGENT_SANDBOX_MANAGER_TOKEN", "AGENT_SANDBOX_MANAGER_TOKEN_NEXT"})
#: The only services it may recreate.
APPLY_SERVICES = frozenset(
    {"runner", "sandbox-manager", "gateway-tunnel", "mcp-tunnel", "control-plane-tunnel"}
)

APPLY_ENVIRONMENT = r"""
import json, os, re, subprocess, sys, tempfile, time
from pathlib import Path

KEYS = {'AGENT_SANDBOX_MANAGER_TOKEN', 'AGENT_SANDBOX_MANAGER_TOKEN_NEXT'}
SERVICES = {'runner', 'sandbox-manager', 'gateway-tunnel', 'mcp-tunnel', 'control-plane-tunnel'}
FILE = re.compile(r'\.compute-secrets/(gateway|mcp|control)\.key')
STABLE_SECONDS = 15
HEALTH_TIMEOUT = 180

def answer(**values):
    print(json.dumps(values))
    sys.exit(0)

def write_private(path, data):
    descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name + '.')
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise

def rewritten(text, changes, remove):
    kept, seen = [], set()
    for line in text.splitlines():
        name = line.split('=', 1)[0].strip()
        if name in remove:
            continue
        if name in changes:
            kept.append(name + "='" + changes[name] + "'")
            seen.add(name)
        else:
            kept.append(line)
    kept.extend(k + "='" + v + "'" for k, v in changes.items() if k not in seen)
    return ('\n'.join(kept) + '\n').encode()

params = json.load(sys.stdin)
root = Path(params['root'])
marker = root / '.compute-identity.json'
if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != params['identity']:
    answer(applied=False, reason='deployment_identity_mismatch')
changes, remove = params.get('changes', {}), params.get('remove', [])
files, services = params.get('files', {}), params.get('services', [])
if (
    set(changes) - KEYS or set(remove) - KEYS or set(services) - SERVICES
    or not all(FILE.fullmatch(name) for name in files)
    or any(c in value for value in changes.values() for c in ("\n", "\r", "'", "\x00"))
):
    answer(applied=False, reason='apply_refused')

environment = os.environ.copy()
environment.update({'DOCKER_HOST': 'unix:///var/run/docker.sock', 'DOCKER_CONTEXT': 'default'})
compose = ['docker', 'compose', '-p', params['project'], '-f', 'deploy/host/docker-compose.sandbox.yml', '--env-file', '.env']

def run(*args, **kwargs):
    return subprocess.run(args, cwd=root, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, **kwargs)

def recreate():
    for service in ('runner', 'sandbox-manager'):
        if service in services:
            found = run(*compose, 'ps', '-q', service)
            if found.returncode:
                return False
            if found.stdout.split() and run('docker', 'stop', '--time=-1', *found.stdout.split()).returncode:
                return False
    return run(*compose, 'up', '-d', '--no-deps', '--force-recreate', '--pull', 'never', *services).returncode == 0

def running():
    listed = run(*compose, 'ps', '--format', 'json', *services)
    if listed.returncode:
        return False
    states = {}
    for line in listed.stdout.splitlines():
        if line.startswith('{'):
            row = json.loads(line)
            states[row.get('Service')] = row.get('State') == 'running' and (row.get('Health') or '').lower() in ('', 'healthy')
    return all(states.get(service) for service in services)

def stayed_up():
    stable = params.get('stable_seconds', STABLE_SECONDS)
    deadline, since = time.monotonic() + max(stable * 4, HEALTH_TIMEOUT if stable else 2), None
    while time.monotonic() < deadline:
        if running():
            since = since or time.monotonic()
            if time.monotonic() - since >= stable:
                return True
        else:
            since = None
        time.sleep(min(2, stable or 0.05))
    return False

env_path = root / '.env'
backups = {env_path: env_path.read_bytes()}
for name in files:
    path = root / name
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
        answer(applied=False, reason='apply_refused')
    backups[path] = path.read_bytes()
write_private(env_path, rewritten(backups[env_path].decode(), changes, set(remove)))
for name, content in files.items():
    write_private(root / name, content.encode())
if services and (not recreate() or not stayed_up()):
    for path, content in backups.items():
        write_private(path, content)
    answer(applied=False, reason='services_unhealthy', restored=recreate() and stayed_up())
answer(applied=True)
"""

__all__ = ["APPLY_ENVIRONMENT", "APPLY_KEYS", "APPLY_SERVICES"]
