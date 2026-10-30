# Deployment agent

The deployment agent lets **Admin → Deployment** manage a self-managed machine:
its deployment configuration, its release, its Kata version, and the
key-rotation and database steps that need the machine. It runs on the machine,
outside the database the control plane uses, and keeps everything it needs to
work — and to recover the machine — under `<DEPLOY_ROOT>/agent/`.

Managed compute servers do not need it: the control plane already deploys and
upgrades them over SSH.

## What it can do

Only the operations in `apps/backend/cloud_agent_shared/deployment_contract.py`:
apply or roll back a configuration version, recreate named services, run a
verified release's `deploy.sh --phase`, install a reviewed Kata version, and the
steps of the key-rotation and database workflows. There is no command, path,
URL or Compose content it accepts from the control plane. Values that would
weaken sandbox isolation are not in the schema; boundary values must be listed
in this machine's `policy.json`, and protected network ranges can only grow.

## Set up

```bash
cd /opt/cloud-agent/release/current          # an extracted recipe
python -m pip install -r deploy/agent/requirements.txt
python -m deploy.agent --root "$DEPLOY_ROOT" init --role control-plane \
    --name cp-1 --control-plane-url https://agents.example.org/api
# Admin → Deployment → Enroll a machine gives a one-time token:
python -m deploy.agent --root "$DEPLOY_ROOT" register --token cde_...
python -m deploy.agent --root "$DEPLOY_ROOT" run
```

`init` records the existing `.env` as configuration version 0 and never rewrites
a line it does not manage. Edit `<DEPLOY_ROOT>/agent/policy.json` on the machine:

```json
{
  "compose": {"project": "cloud-agent", "files": ["/srv/cloud-agent-control/docker-compose.yml"]},
  "allowed": {"AGENT_OPERATION_PROFILING_MODE": ["disabled", "admin"]},
  "kata": [{"version": "3.32.0", "sha256": "1449ecea…"}],
  "kata_helper": ["sudo", "-n", "/opt/cloud-agent/libexec/kata-install-helper.sh"],
  "python": "/opt/cloud-agent/deployer/bin/python",
  "release_lock_file": "/etc/cloud-agent/release-lock.json",
  "database_tools": {
    "psql": ["docker", "run", "--rm", "-i", "--network", "host", "-e", "PGHOST", "-e", "PGPORT", "-e", "PGUSER", "-e", "PGPASSWORD", "-e", "PGDATABASE", "postgres:15", "psql"],
    "pg_dump": ["docker", "run", "--rm", "-i", "--network", "host", "-e", "PGHOST", "-e", "PGPORT", "-e", "PGUSER", "-e", "PGPASSWORD", "-e", "PGDATABASE", "postgres:15", "pg_dump"],
    "pg_restore": ["docker", "run", "--rm", "-i", "--network", "host", "-e", "PGHOST", "-e", "PGPORT", "-e", "PGUSER", "-e", "PGPASSWORD", "-e", "PGDATABASE", "postgres:15", "pg_restore"]
  },
  "tunnel_authorized_keys": {
    "mcp": {"path": "/home/ca-mcp/.ssh/authorized_keys", "options": "command=\"\",restrict,port-forwarding,permitopen=\"127.0.0.1:8001\""}
  },
  "report_services": ["control-plane", "mcp-relay", "web"]
}
```

The control plane can only read this file's summary. Changing a boundary is a
change on the machine. `tunnel_authorized_keys` names, on a control-plane machine,
the file and the fixed restrictions a tunnel key rotation may use; any tunnel not
listed asks an administrator to change its far end by hand. Tunnel key rotation
on a host needs `ssh` and `ssh-keygen` on the agent's `PATH`.

## Releases

A release is usable only after it is imported with its manifest pinned:

```bash
python -m deploy.agent --root "$DEPLOY_ROOT" release import \
    --manifest release.json --recipe recipe.tar.gz --sha256 <pinned manifest sha256>
```

or with the manifest listed in the `release_lock_file` a downstream
distribution reviews. Both digests are checked again before every phase.

## Without the control plane

Every operation has a local command that runs through the same checks and
journal: `status`, `apply NAME=VALUE`, `apply --secret NAME`, `rollback --rev N`,
`restart SERVICE`, `release phase --sha256 … --phase activate`, `kata`,
`journal`, `database status|unfence|resume`. They need no credential: whoever
can run them can already edit `.env`. When the control plane is back, the agent
reports what was done locally; the Admin page shows it as run on the machine
and does not undo it.

A restart settles what it interrupted: a database move continues from its
journal by itself, with both connections sealed to this agent's key, and any
other operation is reported as interrupted. A result the control plane could
not receive stays in the journal and is sent again.

## Backups

Keep `<DEPLOY_ROOT>/agent/` in the recovery set with the database backups: it
holds the configuration history, the release lock, the agent key and the
journal of any rotation or migration in progress.
