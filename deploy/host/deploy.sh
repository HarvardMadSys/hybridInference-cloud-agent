#!/usr/bin/env bash
#
# Deploy the runner host — runners, workspace broker, egress proxy, tunnels.
#
# The other half of `deploy/control-plane/deploy.sh`. They are separate because
# they are separate machines: the control plane holds the queue and the web app,
# this one executes jobs, and each is deployed by a runner installed on itself.
#
# **Thin on purpose.** `sandbox_host.sh up` already is the bring-up: it
# checks the daemon, requires the environment, runs the isolation preflight
# fail-closed, builds the sandbox image, starts the stack, and then probes the
# gateway *from inside the closed network* — the only vantage point whose answer
# means anything. Re-implementing any of that here would give the deploy a
# second opinion about whether the host is fit to run untrusted code, and the
# two would drift.
#
# Usage:  deploy.sh <sha>
#   DEPLOY_ROOT   required — where the deployed checkout lives. Deliberately
#                 without a default: this path differs per machine (the host
#                 runs as a user without write access to /srv) and a wrong
#                 guess would deploy into a directory nobody is serving from.
#   SOURCE_REPO   a local clone holding <sha> (default: $GITHUB_WORKSPACE)
#   REPLICAS      runner count (default: whatever .env says, else the script's)
#   COMPOSE_PROJECT  this stack's compose project (default: from the deploy root's .env)
set -euo pipefail

SHA="${1:-${DEPLOY_SHA:-}}"
SOURCE_REPO="${SOURCE_REPO:-${GITHUB_WORKSPACE:-}}"
DEPLOY_ROOT="${DEPLOY_ROOT:-}"
DEPLOY_PROTOCOL="shared-live-exact-sha-v2"

log() { printf '[deploy-host] %s\n' "$*"; }
die() { printf '[deploy-host] ERROR: %s\n' "$*" >&2; exit 1; }

[ -n "${SHA}" ] || die "no commit given: deploy.sh <sha>"
[ -n "${DEPLOY_ROOT}" ] || die "DEPLOY_ROOT is not set (repository variable AGENT_HOST_DEPLOY_ROOT)"

cd "${DEPLOY_ROOT}" 2>/dev/null || die "${DEPLOY_ROOT} does not exist"

git rev-parse --git-dir >/dev/null 2>&1 \
  || die "${DEPLOY_ROOT} is not a git checkout — convert it once first (deploy/control-plane/README-cd.md)"

# Same guard, same reason as the control plane's: this tree was populated by
# rsync, and on 2026-08-06 it still held files from two branches that had been
# deleted from the remote without ever being proposed. One of them was the fix
# for a live sign-in failure. A deploy that silently overwrote them would have
# destroyed the only copy.
DIRTY="$(git status --porcelain --untracked-files=no)"
if [ -n "${DIRTY}" ]; then
  printf '[deploy-host] local modifications in %s:\n%s\n' "${DEPLOY_ROOT}" "${DIRTY}" >&2
  if [ "${ALLOW_DIRTY:-0}" != "1" ]; then
    die "refusing to overwrite them. Get them into git, or re-run with ALLOW_DIRTY=1 to discard."
  fi
  log "ALLOW_DIRTY=1 — discarding the above"
fi

[ -f .env ] || die ".env is missing from ${DEPLOY_ROOT} — it holds this host's credentials and is deliberately not in git"

# Fetched from a clone on this machine: the runner's workspace is already a
# checkout of exactly this commit, so the host needs no git credential of its
# own for a private repository.
[ -n "${SOURCE_REPO}" ] \
  || die "no SOURCE_REPO and no GITHUB_WORKSPACE — point SOURCE_REPO at a local clone holding ${SHA}"
SOURCE_HEAD="$(git -C "${SOURCE_REPO}" rev-parse HEAD)"
[ "${SOURCE_HEAD}" = "${SHA}" ] \
  || die "SOURCE_REPO is at ${SOURCE_HEAD}, not the requested ${SHA} — refusing to deploy something other than what was asked for"
TARGET_DEPLOY_PROTOCOL="$(git -C "${SOURCE_REPO}" show "${SHA}:deploy/DEPLOY_PROTOCOL" 2>/dev/null || true)"
[ "${TARGET_DEPLOY_PROTOCOL}" = "${DEPLOY_PROTOCOL}" ] \
  || die "target ${SHA} does not implement ${DEPLOY_PROTOCOL}; it is pre-fence or protocol-incompatible, so online deployment was refused before changing the host"

PREVIOUS="$(git rev-parse HEAD 2>/dev/null || echo unknown)"
log "previous: ${PREVIOUS}"
log "target:   ${SHA}"

git fetch --quiet "${SOURCE_REPO}" HEAD
git reset --hard --quiet FETCH_HEAD
NOW="$(git rev-parse HEAD)"
[ "${NOW}" = "${SHA}" ] || die "checkout landed on ${NOW}, expected ${SHA}"
log "checkout now at ${NOW}"

# Every heartbeat carries this exact commit only after sandbox_host.sh has
# proved all runner replicas and the broker. The control-plane deployment uses
# it as a rolling-compatibility fence; it is not a topology switch.
AGENT_DEPLOYMENT_SHA="${SHA}"
export AGENT_DEPLOYMENT_SHA

# Which stack this deploy owns. Read the same way `sandbox_host.sh` reads it,
# from the environment or from this deploy root's `.env`, so the guard below and
# the bring-up can never disagree about which containers are ours.
from_env() {
  local value
  value="$(grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
  # Match Compose and sandbox_host.sh: one quoted pair is syntax, not part of
  # a project name. Without this, COMPOSE_PROJECT="host-prod" makes this guard
  # inspect a different stack from the one sandbox_host.sh subsequently owns.
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}
COMPOSE_PROJECT="${COMPOSE_PROJECT:-$(from_env COMPOSE_PROJECT)}"
[ -n "${COMPOSE_PROJECT}" ] \
  || die "COMPOSE_PROJECT is not set — put it in ${DEPLOY_ROOT}/.env (e.g. freeinference-cloud-agent-sandbox-staging) or pass it in the environment"
export COMPOSE_PROJECT

# A second stack of *this* deployment under a different compose project name is
# the failure this catches, and it is not hypothetical: this host was brought up
# by hand as project `agenthost` while the script used
# `hybridinference-agent-remote`. Deploying produced *two* complete stacks — six
# runners claiming instead of three, and two workspace brokers.
#
# The brokers are the dangerous half. The control plane stores one `broker_url`
# per host, so whichever stack heartbeats last wins it, and a job claimed by a
# runner in one stack has its files and terminal served from the other. That
# reads as "the workspace is empty", not as "there are two of me".
#
# **A sibling environment is not a stray.** Staging and production share this
# machine deliberately, each with its own project, `.env` and control plane.
# Name the other one in `SIBLING_COMPOSE_PROJECTS` (comma-separated) and it stops
# being reported; anything still unrecognised is the accident this exists for.
EXPECTED_PROJECT="${COMPOSE_PROJECT}"
# From the environment or from this host's `.env`, like everything else here. A
# variable the deploy reads from only one of the two is a line an operator can
# write into `.env`, restart, and watch do nothing.
SIBLING_COMPOSE_PROJECTS="${SIBLING_COMPOSE_PROJECTS:-$(from_env SIBLING_COMPOSE_PROJECTS)}"
KNOWN="${EXPECTED_PROJECT},${SIBLING_COMPOSE_PROJECTS}"
STRAY="$(docker ps --format '{{.Label "com.docker.compose.project"}} {{.Names}}' 2>/dev/null \
  | awk -v known="${KNOWN}" '
      BEGIN { n = split(known, k, ","); for (i = 1; i <= n; i++) if (k[i] != "") ok[k[i]] = 1 }
      !($1 in ok) && $2 ~ /-(runner|workspace-broker|gateway-tunnel|mcp-tunnel|control-plane-tunnel|agent-egress-proxy)-[0-9]+$/ {
        print "  " $2 "  (project: " $1 ")"
      }')"
if [ -n "${STRAY}" ]; then
  printf '[deploy-host] containers from another compose project are running this same stack:\n%s\n' "${STRAY}" >&2
  if [ "${ALLOW_STRAY_STACK:-0}" != "1" ]; then
    die "refusing to add a second stack beside them. If that is the other environment, add its project to SIBLING_COMPOSE_PROJECTS. Otherwise stop it first:
  docker compose -p <that project> -f deploy/host/docker-compose.sandbox.yml --env-file .env down
Or set ALLOW_STRAY_STACK=1 if you genuinely want both."
  fi
  log "ALLOW_STRAY_STACK=1 — starting anyway"
fi

# Everything that makes this a deployment rather than a `git pull` lives in
# there, including the refusal to start runners on a host that cannot isolate
# them. It exits non-zero on a failed gateway probe, which is what turns a
# broken tunnel into a red deploy instead of jobs that claim and then fail.
log "handing over to sandbox_host.sh up"
if [ -n "${REPLICAS:-}" ]; then
  DEFER_SHARED_READY_MARKER=1 bash deploy/host/sandbox_host.sh up "${REPLICAS}"
else
  DEFER_SHARED_READY_MARKER=1 bash deploy/host/sandbox_host.sh up
fi

printf '%s\n' "${SHA}" > .deployed-sha
bash deploy/host/sandbox_host.sh mark-ready "${SHA}"
log "deployed ${SHA} (was ${PREVIOUS})"
