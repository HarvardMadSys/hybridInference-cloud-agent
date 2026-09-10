#!/usr/bin/env bash
#
# Deploy the control plane, web app and MCP relay to this machine.
#
# Runs *on* the deployment host. The CI runners are installed on spark2, which
# is also where this stack lives and is owned by the same user, so the workflow
# calling this needs no SSH key, no known_hosts secret and no sudo — the whole
# apparatus the gateway's deploy carries exists because its runners are on a
# different machine from its target.
#
# Idempotent: running it twice with the same SHA is a no-op that still proves
# the stack is healthy.
#
# Usage:  deploy.sh <sha>
#   SOURCE_REPO   a local clone holding <sha> (default: $GITHUB_WORKSPACE)
#   DEPLOY_ROOT   where the deployed checkout lives (default /srv/freeinference-cloud-agent)
#   COMPOSE_PROJECT  compose project name (default cloudagent)
#   ALLOW_DIRTY=1 deploy over local modifications instead of refusing
set -euo pipefail

SHA="${1:-${DEPLOY_SHA:-}}"
SOURCE_REPO="${SOURCE_REPO:-${GITHUB_WORKSPACE:-}}"
DEPLOY_ROOT="${DEPLOY_ROOT:-/srv/freeinference-cloud-agent}"
COMPOSE_PROJECT="${COMPOSE_PROJECT:-cloudagent}"
COMPOSE_FILE="deploy/control-plane/docker-compose.yml"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-180}"
SHARED_READY_TIMEOUT="${SHARED_READY_TIMEOUT:-900}"
DEPLOY_PROTOCOL="shared-live-exact-sha-v2"

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[ -n "${SHA}" ] || die "no commit given: deploy.sh <sha>"
[[ "${HEALTH_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] \
  || die "HEALTH_TIMEOUT must be a positive integer (got '${HEALTH_TIMEOUT}')"
[[ "${SHARED_READY_TIMEOUT}" =~ ^[1-9][0-9]*$ ]] \
  || die "SHARED_READY_TIMEOUT must be a positive integer (got '${SHARED_READY_TIMEOUT}')"

cd "${DEPLOY_ROOT}" 2>/dev/null || die "${DEPLOY_ROOT} does not exist"

# This directory was an rsync drop until CD existed, and `git reset --hard`
# against a non-checkout silently does nothing useful. Fail rather than deploy
# something that is not what the caller asked for.
git rev-parse --git-dir >/dev/null 2>&1 \
  || die "${DEPLOY_ROOT} is not a git checkout — convert it once before enabling CD (see deploy/control-plane/README-cd.md)"

# The reason this check exists is #29: four files that only ever existed on this
# host were found by diffing it against dev, and a rebuild would have reverted
# both of the fixes in them without a word. `git reset --hard` overwrites
# modified tracked files. Untracked files (.env, .deployed-sha) it leaves alone,
# which is why they are not in this check.
DIRTY="$(git status --porcelain --untracked-files=no)"
if [ -n "${DIRTY}" ]; then
  printf '[deploy] local modifications in %s:\n%s\n' "${DEPLOY_ROOT}" "${DIRTY}" >&2
  if [ "${ALLOW_DIRTY:-0}" != "1" ]; then
    die "refusing to overwrite them. Get them into git, or re-run with ALLOW_DIRTY=1 to discard."
  fi
  log "ALLOW_DIRTY=1 — discarding the above"
fi

[ -f .env ] || die ".env is missing from ${DEPLOY_ROOT} — it holds this deployment's secrets and is deliberately not in git"

# Read one value from the deployment's persistent, untracked .env.
from_env() {
  local value
  value="$(grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

PREVIOUS="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
log "previous: ${PREVIOUS}"
log "target:   ${SHA}"

# Fetched from a clone on this machine rather than from GitHub. The runner's
# workspace is already a full checkout of exactly this commit, authenticated by
# the job's own token, and it is on the same disk — so the deployment host needs
# no credential of its own for a private repository. A deploy key here would be
# a standing secret on the box, existing only to re-download something already
# present a few directories away.
[ -n "${SOURCE_REPO}" ] \
  || die "no SOURCE_REPO and no GITHUB_WORKSPACE — point SOURCE_REPO at a local clone holding ${SHA}"
[ -d "${SOURCE_REPO}/.git" ] || [ -f "${SOURCE_REPO}/.git" ] \
  || die "SOURCE_REPO=${SOURCE_REPO} is not a git repository"

SOURCE_HEAD="$(git -C "${SOURCE_REPO}" rev-parse HEAD)"
[ "${SOURCE_HEAD}" = "${SHA}" ] \
  || die "SOURCE_REPO is at ${SOURCE_HEAD}, not the requested ${SHA} — refusing to deploy something other than what was asked for"
TARGET_DEPLOY_PROTOCOL="$(git -C "${SOURCE_REPO}" show "${SHA}:deploy/DEPLOY_PROTOCOL" 2>/dev/null || true)"
[ "${TARGET_DEPLOY_PROTOCOL}" = "${DEPLOY_PROTOCOL}" ] \
  || die "target ${SHA} does not implement ${DEPLOY_PROTOCOL}; it is pre-fence or protocol-incompatible, so online deployment was refused before changing the deployment"

log "fetching ${SHA} from ${SOURCE_REPO}"
git fetch --quiet "${SOURCE_REPO}" HEAD
git reset --hard --quiet FETCH_HEAD

NOW="$(git rev-parse HEAD)"
[ "${NOW}" = "${SHA}" ] || die "checkout landed on ${NOW}, expected ${SHA}"
log "checkout now at ${NOW}"

# The target API uses the same exact generation as the deployment waiter when
# deciding whether a replica may materialize a new retained group. Export it
# for every Compose invocation, including a bounded cold-start bootstrap.
AGENT_DEPLOYMENT_SHA="${SHA}"
export AGENT_DEPLOYMENT_SHA

compose() {
  docker compose -p "${COMPOSE_PROJECT}" --env-file .env -f "${COMPOSE_FILE}" "$@"
}

# Ports come from .env when set; these defaults match the compose file.  The
# control-plane port is needed before the compatibility fence only on a true
# cold start, where there is no old API through which the newly deployed host
# can publish its post-deploy heartbeat.
CP_PORT="$(from_env AGENT_CONTROL_PLANE_PORT)"; CP_PORT="${CP_PORT:-8000}"
MCP_PORT="$(from_env AGENT_MCP_RELAY_PORT)"; MCP_PORT="${MCP_PORT:-8001}"
WEB_PORT="$(from_env AGENT_WEB_PORT)";          WEB_PORT="${WEB_PORT:-3002}"
EDGE_PORT="$(from_env AGENT_EDGE_PORT)";        EDGE_PORT="${EDGE_PORT:-3003}"
ACTIVE_SERVICES="$(compose config --services)"

wait_for() {
  local name="$1" url="$2" deadline=$((SECONDS + HEALTH_TIMEOUT))
  log "waiting for ${name} at ${url}"
  while [ "${SECONDS}" -lt "${deadline}" ]; do
    if curl -fsS --max-time 5 "${url}" >/dev/null 2>&1; then
      log "${name}: ok"
      return 0
    fi
    sleep 3
  done
  printf '[deploy] %s never became healthy; last 40 lines per service:\n' "${name}" >&2
  compose logs --tail 40 >&2 || true
  die "${name} health check timed out after ${HEALTH_TIMEOUT}s (deployed ${SHA}, previous ${PREVIOUS})"
}

# A normal rolling deployment keeps the old API and MCP relay alive until the
# target host is proved. After `compose down` either service may independently
# be absent: the host needs the API for its fresh heartbeat and both sandbox
# relays for its exact-SHA probe. Track the services this invocation starts so
# a failed deploy restores the *actual* prior state instead of stopping a
# healthy service that happened to survive.
BOOTSTRAP_SERVICES=()
cleanup_failed_bootstrap() {
  local status=$?
  if [ "${status}" -ne 0 ] && [ "${#BOOTSTRAP_SERVICES[@]}" -gt 0 ]; then
    log "deployment failed; restoring previously stopped bootstrap services"
    compose stop "${BOOTSTRAP_SERVICES[@]}" >/dev/null 2>&1 || true
  fi
}
trap cleanup_failed_bootstrap EXIT

log "building images"
compose build

# Readiness itself uses the agent-owned database. Bring up only that durable
# dependency and migrate it before evaluating the host fence. This ordering is
# required on a cold machine (and after `compose down`): `compose run --no-deps`
# cannot discover a Postgres container that does not exist, while putting the
# full `compose up` first would replace the API before compatibility was proved.
# Migrations shipped here must therefore remain backwards-compatible with the
# still-running API for the duration of this rolling-deploy window.
log "starting deployment database"
# `--wait` makes the healthcheck above an executable ordering guarantee. A
# plain detached `up` returns as soon as the container has started, which can
# still let Alembic race Postgres initialization on a fresh volume.
compose up -d --wait --wait-timeout "${HEALTH_TIMEOUT}" postgres
log "migrating deployment database"
compose run --rm --no-deps migrate

# Check the identity boundary before bootstrapping or replacing either listener.
# Historical data is never adopted by CD: an operator must first acknowledge
# its original issuer. A refusal here leaves the running API and relay intact.
log "checking the Agent database identity issuer"
compose run --rm --no-deps control-plane \
  python -m cloud_agent.identity_namespace --check

# Heartbeats are HTTP calls to the control plane, not direct database writes.
# Preserve the old API on an ordinary rolling deploy.  Only when no API
# container is running may this target generation be started early so a host
# coming up in parallel can enroll/heartbeat and release the compatibility
# fence.  There is no older service to replace in this branch; user work stays
# queued until an eligible host appears.  The EXIT trap above restores the
# services this invocation bootstrapped if their health or the fence fails.
RUNNING_CONTROL_ID="$(compose ps --status running -q control-plane 2>/dev/null || true)"
RUNNING_MCP_ID="$(compose ps --status running -q mcp-relay 2>/dev/null || true)"
if [ -z "${RUNNING_CONTROL_ID}" ]; then
  BOOTSTRAP_SERVICES+=(control-plane)
fi
if [ -z "${RUNNING_MCP_ID}" ]; then
  BOOTSTRAP_SERVICES+=(mcp-relay)
fi
if [ "${#BOOTSTRAP_SERVICES[@]}" -gt 0 ]; then
  log "starting missing target services for parallel host readiness: ${BOOTSTRAP_SERVICES[*]}"
  # Host readiness probes both fixed sandbox relays. Starting only the API on
  # a clean machine, or assuming MCP is running merely because the API is,
  # deadlocks deployment: the host cannot publish its exact-SHA marker while
  # the full compose up that would start the missing service is fenced on that
  # marker. The MCP app exposes only the scoped relay surface and both services
  # are loopback-bound; browser-facing web remains stopped until the gate.
  compose up -d --no-deps "${BOOTSTRAP_SERVICES[@]}"
fi
if [ -z "${RUNNING_CONTROL_ID}" ]; then
  wait_for "bootstrap control plane" "http://127.0.0.1:${CP_PORT}/healthz"
fi
if [ -z "${RUNNING_MCP_ID}" ]; then
  wait_for "bootstrap MCP relay" "http://127.0.0.1:${MCP_PORT}/healthz"
fi

# The retained/shared-live topology is the only executable path, so there is
# no old control-plane/runner protocol to fall back to during a rolling
# deployment. The host and control jobs start in parallel. The host publishes
# the exact target SHA only after its runners, broker, tunnel and Docker data
# root pass their local checks; this reverse probe then waits for that fact and
# proves the control plane can dial every eligible broker before replacing live
# API containers. It is a compatibility fence, not a feature switch.
log "waiting for target ${SHA} host readiness and reverse broker probes"
compose run --rm --no-deps control-plane \
  python -m cloud_agent.deploy_readiness \
    --timeout-seconds "${SHARED_READY_TIMEOUT}" \
    --expected-deployment-sha "${SHA}"

# The compatibility gate passed, so any bootstrap peers are now part of the
# desired target stack rather than temporary readiness aids. A later full-stack
# startup/health failure must not stop API/MCP selectively while leaving web or
# another replacement service running.
BOOTSTRAP_SERVICES=()

# `up -d` with no service list brings the whole stack to the file's desired
# state, which is what makes this idempotent. Compose may run the idempotent
# migration service again; the API still waits on its successful completion.
log "starting"
compose up -d --remove-orphans

# The web app is mounted under a base path, so `/` is a 404 by design and a
# health check pointed at it fails every deploy of a working app. Checked
# against the running staging host: `/` → 404, `/agents` → 200, `/agents/` →
# 308. The un-slashed form is the one to probe.
WEB_BASE_PATH="$(from_env AGENT_WEB_BASE_PATH)"
WEB_BASE_PATH="${WEB_BASE_PATH%/}"

wait_for "control plane" "http://127.0.0.1:${CP_PORT}/healthz"
wait_for "MCP relay"     "http://127.0.0.1:${MCP_PORT}/healthz"
wait_for "web"           "http://127.0.0.1:${WEB_PORT}${WEB_BASE_PATH:-/}"
if grep -qx edge <<<"${ACTIVE_SERVICES}"; then
  wait_for "edge web route" "http://127.0.0.1:${EDGE_PORT}/"
  wait_for "edge API route" "http://127.0.0.1:${EDGE_PORT}/api/healthz"
fi

# Written last and only on success: this file is what an operator reads to
# answer "what is actually running here", and a value written before the health
# check would answer it wrongly for every failed deploy.
printf '%s\n' "${SHA}" > .deployed-sha
log "deployed ${SHA} (was ${PREVIOUS})"
