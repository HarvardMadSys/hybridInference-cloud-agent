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

log() { printf '[deploy] %s\n' "$*"; }
die() { printf '[deploy] ERROR: %s\n' "$*" >&2; exit 1; }

[ -n "${SHA}" ] || die "no commit given: deploy.sh <sha>"

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

# Read values the same way Compose will: an explicit process value wins over
# this deployment's persistent, untracked .env. Defined before bring-up because
# staging's shared-live scheduling gate is deployment policy, not a secret.
from_env() {
  local value
  value="$(grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

# GitHub runs a workflow_run from the default branch's workflow definition,
# while this script comes from the dev commit being deployed. Recognise the old
# workflow by its stable name so the commit that adds staging rollout policy can
# activate itself immediately; the updated workflow also passes the explicit
# environment name for every later run.
DEPLOYMENT_ENVIRONMENT="${DEPLOYMENT_ENVIRONMENT:-$(from_env DEPLOYMENT_ENVIRONMENT)}"
if [ -z "${DEPLOYMENT_ENVIRONMENT}" ] && [ "${GITHUB_WORKFLOW:-}" = "Deploy Staging" ]; then
  DEPLOYMENT_ENVIRONMENT=staging
fi
if [ "${DEPLOYMENT_ENVIRONMENT}" = "staging" ]; then
  staging_rollout="${STAGING_SHARED_LIVE_SANDBOX:-$(from_env STAGING_SHARED_LIVE_SANDBOX)}"
  if [ -z "${staging_rollout}" ] && [ "${AGENT_SHARED_LIVE_SANDBOX+x}" = x ]; then
    staging_rollout="${AGENT_SHARED_LIVE_SANDBOX}"
  fi
  # Ignore an old generic default-off line in staging's persistent .env. The
  # environment-specific override above remains available during the one run
  # where GitHub still evaluates the workflow definition from main.
  AGENT_SHARED_LIVE_SANDBOX="${staging_rollout:-1}"
  export AGENT_SHARED_LIVE_SANDBOX
  log "staging shared-live scheduling gate: ${AGENT_SHARED_LIVE_SANDBOX}"
else
  shared_live="${AGENT_SHARED_LIVE_SANDBOX:-$(from_env AGENT_SHARED_LIVE_SANDBOX)}"
  AGENT_SHARED_LIVE_SANDBOX="${shared_live:-0}"
fi
case "${AGENT_SHARED_LIVE_SANDBOX}" in
  0|1) ;;
  *) die "AGENT_SHARED_LIVE_SANDBOX must be 0 or 1, got '${AGENT_SHARED_LIVE_SANDBOX}'" ;;
esac
case "${SHARED_READY_TIMEOUT}" in
  ''|*[!0-9]*) die "SHARED_READY_TIMEOUT must be a positive integer, got '${SHARED_READY_TIMEOUT}'" ;;
  0) die "SHARED_READY_TIMEOUT must be greater than zero" ;;
esac
export AGENT_SHARED_LIVE_SANDBOX

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

log "fetching ${SHA} from ${SOURCE_REPO}"
git fetch --quiet "${SOURCE_REPO}" HEAD
git reset --hard --quiet FETCH_HEAD

NOW="$(git rev-parse HEAD)"
[ "${NOW}" = "${SHA}" ] || die "checkout landed on ${NOW}, expected ${SHA}"
log "checkout now at ${NOW}"

compose() {
  docker compose -p "${COMPOSE_PROJECT}" --env-file .env -f "${COMPOSE_FILE}" "$@"
}

log "building images"
compose build

# GitHub evaluates `workflow_run` from the default branch, so the first dev
# merge carrying `needs: deploy-host` still runs the older parallel workflow.
# Do not let that bootstrap race turn scheduling on while its host job is still
# building — or after that job has failed.  A one-off container from the target
# image reads the existing database and waits for a capability heartbeat newer
# than its own start, then dials the reported broker from the control-plane
# network with the real broker credential.  Nothing running is replaced until
# both proofs pass.  The same barrier remains useful after the workflow update:
# it turns the job dependency into an end-to-end assertion rather than trust in
# a green job name.
if [ "${AGENT_SHARED_LIVE_SANDBOX}" = "1" ]; then
  log "waiting for a fresh shared-live host heartbeat and control-plane-to-broker probe"
  compose run --rm --no-deps control-plane \
    python -m cloud_agent.deploy_readiness \
      --timeout-seconds "${SHARED_READY_TIMEOUT}" \
      --expected-deployment-sha "${SHA}"
fi

# `up -d` with no service list brings the whole stack to the file's desired
# state, which is what makes this idempotent. It also runs `migrate` — the
# compose file has control-plane wait on `service_completed_successfully`, so
# alembic finishes before anything reads the schema it just changed.
log "starting"
compose up -d --remove-orphans

# Ports come from .env when set; these defaults match the compose file's.
CP_PORT="$(from_env AGENT_CONTROL_PLANE_PORT)"; CP_PORT="${CP_PORT:-8000}"
WEB_PORT="$(from_env AGENT_WEB_PORT)";          WEB_PORT="${WEB_PORT:-3002}"

# The web app is mounted under a base path, so `/` is a 404 by design and a
# health check pointed at it fails every deploy of a working app. Checked
# against the running staging host: `/` → 404, `/agents` → 200, `/agents/` →
# 308. The un-slashed form is the one to probe.
WEB_BASE_PATH="$(from_env AGENT_WEB_BASE_PATH)"
WEB_BASE_PATH="${WEB_BASE_PATH%/}"

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

wait_for "control plane" "http://127.0.0.1:${CP_PORT}/healthz"
wait_for "web"           "http://127.0.0.1:${WEB_PORT}${WEB_BASE_PATH:-/}"

# Written last and only on success: this file is what an operator reads to
# answer "what is actually running here", and a value written before the health
# check would answer it wrongly for every failed deploy.
printf '%s\n' "${SHA}" > .deployed-sha
log "deployed ${SHA} (was ${PREVIOUS})"
