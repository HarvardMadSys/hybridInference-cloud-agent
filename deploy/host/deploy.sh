#!/usr/bin/env bash
#
# Deploy the runner host — runners, sandbox manager, tunnels.
#
# The other half of `deploy/control-plane/deploy.sh`. They are separate because
# they are separate machines: the control plane holds the queue and the web app,
# this one executes jobs, and each is deployed by a runner installed on itself.
#
# **Thin on purpose.** `sandbox_host.sh up` already is the bring-up: it
# checks the daemon, requires the environment, runs the isolation preflight
# fail-closed, builds the sandbox image, starts the stack, and then probes the
# gateway *from the relay network* the sandbox groups are handed their relays
# from, and has every runner install and verify this deployment's host rules.
# Re-implementing any of that here would give the deploy a second opinion about
# whether the host is fit to run untrusted code, and the two would drift.
#
# Usage:  deploy.sh [--phase prepare|activate] <sha>
#
# With no `--phase` this is the whole rollout, which is what every caller before
# the split invoked.
#
# The phases are `sandbox_host.sh`'s two halves, with this script's own guards
# and recovery around them. They exist because the release design sequences the
# control plane's database preparation *between* them (#270):
#
#   prepare   the stray-stack guard, then everything the rollout does while the
#             generation it replaces is still serving — pull or build, the Kata
#             smoke test, the candidate kernel and observer check.
#   activate  drain, replace, prove, record, and the fallback to the recorded
#             generation if that fails.
#
#   DEPLOY_ROOT   required — where the deployed checkout lives. Deliberately
#                 without a default: this path differs per machine (the host
#                 runs as a user without write access to /srv) and a wrong
#                 guess would deploy into a directory nobody is serving from.
#   SOURCE_REPO   a local clone holding <sha> (default: $GITHUB_WORKSPACE)
#   COMPOSE_PROJECT  this stack's compose project (default: from the deploy root's .env)
#   AGENT_DEPLOY_MODE=artifacts uses AGENT_RELEASE_MANIFEST and the packaged
#   recipe; DEPLOY_ROOT remains the persistent deployment/configuration root.
set -euo pipefail

AGENT_DEPLOY_MODE="${AGENT_DEPLOY_MODE:-source}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SANDBOX_HOST_SCRIPT="deploy/host/sandbox_host.sh"
PHASE=all
while [ "$#" -gt 0 ]; do
  case "$1" in
    --phase)
      [ "$#" -ge 2 ] || { printf '[deploy-host] ERROR: --phase needs a value\n' >&2; exit 1; }
      PHASE="$2"; shift 2 ;;
    --phase=*) PHASE="${1#--phase=}"; shift ;;
    *) break ;;
  esac
done
case "${PHASE}" in
  all|prepare|activate) ;;
  *) printf '[deploy-host] ERROR: unknown phase %s; expected prepare or activate\n' "${PHASE}" >&2; exit 1 ;;
esac
runs_phase() { [ "${PHASE}" = all ] || [ "${PHASE}" = "$1" ]; }

SHA="${1:-${DEPLOY_SHA:-}}"
SOURCE_REPO="${SOURCE_REPO:-${GITHUB_WORKSPACE:-}}"
DEPLOY_ROOT="${DEPLOY_ROOT:-}"
DEPLOY_PROTOCOL="shared-live-exact-sha-v4"

log() { printf '[deploy-host] %s\n' "$*"; }
die() { printf '[deploy-host] ERROR: %s\n' "$*" >&2; exit 1; }

case "$AGENT_DEPLOY_MODE" in
  source) [ -n "${SHA}" ] || die "no commit given: deploy.sh <sha>" ;;
  artifacts) ;;
  *) die "AGENT_DEPLOY_MODE must be source or artifacts" ;;
esac
export AGENT_DEPLOY_MODE
[ -n "${DEPLOY_ROOT}" ] || die "DEPLOY_ROOT is not set"

cd "${DEPLOY_ROOT}" 2>/dev/null || die "${DEPLOY_ROOT} does not exist"

# Read before anything in this script can overwrite it. This file is written
# once a rollout has passed every host check, immediately before readiness is
# published, so it names a generation that worked; `PREVIOUS` below does not — in
# source mode it is whatever commit the checkout happened to be standing on,
# which after a failed deploy is the commit that failed. The failure path at the
# bottom is the only reader.
RECORDED="$(cat .deployed-sha 2>/dev/null || true)"

if [[ "$AGENT_DEPLOY_MODE" == "artifacts" ]]; then
  [ -f .env ] || die ".env is missing from ${DEPLOY_ROOT}"
  source "$SCRIPT_DIR/../release/artifact_env.sh"
  prepare_artifact_deployment host
  SANDBOX_HOST_SCRIPT="$SCRIPT_DIR/sandbox_host.sh"
  APP_DIR="$PWD"
  export APP_DIR
  PREVIOUS="$(cat .deployed-sha 2>/dev/null || echo unknown)"
  log "previous: ${PREVIOUS}"
  log "target:   ${SHA}"
else
  git rev-parse --git-dir >/dev/null 2>&1 \
    || die "${DEPLOY_ROOT} is not the git checkout required by AGENT_DEPLOY_MODE=source"

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
fi

# Every heartbeat carries this exact commit only after sandbox_host.sh has
# proved all runner replicas and the sandbox manager. The control-plane deployment uses
# it as a rolling-compatibility fence; it is not a topology switch.
AGENT_DEPLOYMENT_SHA="${SHA}"
export AGENT_DEPLOYMENT_SHA

# Which stack this deploy owns. Read the same way `sandbox_host.sh` reads it,
# from the environment or from this deploy root's `.env`, so the guard below and
# the bring-up can never disagree about which containers are ours.
from_env() {
  local value
  if [[ "$AGENT_DEPLOY_MODE" == "artifacts" ]]; then
    if [[ "${!1+x}" == "x" ]]; then
      printf '%s' "${!1}"
    else
      artifact_env_value "$1"
    fi
    return 0
  fi
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
# runners claiming instead of three, and two sandbox managers.
#
# The sandbox managers are the dangerous half. The control plane stores one `sandbox_manager_url`
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
      !($1 in ok) && $2 ~ /-(runner|sandbox-manager|gateway-tunnel|mcp-tunnel|control-plane-tunnel)-[0-9]+$/ {
        print "  " $2 "  (project: " $1 ")"
      }')"
if [ -n "${STRAY}" ]; then
  printf '[deploy-host] containers from another compose project are running this same stack:\n%s\n' "${STRAY}" >&2
  if [[ "$AGENT_DEPLOY_MODE" == "artifacts" ]]; then
    die "refusing to add a second stack; name legitimate siblings in SIBLING_COMPOSE_PROJECTS or stop the stray stack"
  fi
  if [ "${ALLOW_STRAY_STACK:-0}" != "1" ]; then
    die "refusing to add a second stack beside them. If that is the other environment, add its project to SIBLING_COMPOSE_PROJECTS. Otherwise stop it first:
  docker compose -p <that project> -f deploy/host/docker-compose.sandbox.yml --env-file .env down
Or set ALLOW_STRAY_STACK=1 if you genuinely want both."
  fi
  log "ALLOW_STRAY_STACK=1 — starting anyway"
fi

# ---------------------------------------------------------------------------
# A failed rollout must not leave this host advertising nothing.
#
# `sandbox_host.sh up` is not transactional. It invalidates this host's
# shared-live marker and drains the runners that were serving *before* it runs
# the checks that decide whether the replacement is fit (sandbox_host.sh,
# 902-943: prepare the marker, stop the runners, `compose up -d`, `cmd_check`,
# complete the marker). A check that fails therefore leaves the marker at
# `pending:<sha>`, and a runner advertising no deployment does not poll at all —
# so the queue stalls behind a page that says "waiting to start" and nothing in
# the system says why.
#
# On 2026-09-13 one lost guest-observer probe left staging in exactly that
# state. Nothing retried it, and nothing brought the previous generation back,
# so it lasted 29.6 hours — until an unrelated merge happened to trigger the
# next deploy (issue #259). Retrying the probe is its own change (#257); this is
# the other half, and the half that bounds the damage when a retry is exhausted.
#
# Restoring is a deploy of an older commit and deliberately not a second
# mechanism. It is driven by the commit `.deployed-sha` records instead of by a
# human who first has to notice the deploy failed. This job still exits non-zero
# — a restore is a repair, not a success.
# ---------------------------------------------------------------------------

#: A marker naming a commit is this host advertising a generation. An empty or
#: `pending:` marker is this host advertising nothing, which is the only state
#: worth interrupting a failed deploy to repair: `up` invalidates the marker
#: after the build and the Kata probe, so a rollout that died before that left
#: the previous generation running and must not be disturbed.
advertises_a_generation() {
  local current
  current="$(cat .shared-live-ready 2>/dev/null || true)"
  [[ -n "${current}" && "${current}" != pending:* ]]
}

#: Bring back the recorded generation, using the recorded generation's own
#: script, compose file and build recipe — the same ones that produced the
#: containers it is replacing. Returns non-zero, without touching the tree, when
#: it cannot prove the target is one this host is able to produce.
restore_recorded_generation() {
  local recorded="$1" target_protocol
  if [[ "${AGENT_DEPLOY_MODE}" != "source" ]]; then
    log "cannot restore ${recorded} in ${AGENT_DEPLOY_MODE} mode: this script is handed one release manifest and has no older one to hand itself"
    return 1
  fi
  # Resetting the tree rewrites the file bash is currently reading, because bash
  # executes a script by byte offset rather than from a copy. CI runs this from
  # `.deploy-orchestrator`, a checkout that is not DEPLOY_ROOT, and the
  # documented manual path passes a SOURCE_REPO; running it out of the tree it
  # is about to rewrite is refused rather than half-supported.
  if [[ "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/" == "${DEPLOY_ROOT}/"* ]]; then
    log "refusing to restore from inside ${DEPLOY_ROOT}: a restore resets that tree while this file is still being read. Dispatch the workflow with sha=${recorded} instead."
    return 1
  fi
  # The recorded commit has to be one this tree can produce, not merely a name
  # somebody wrote down. Fetching from the same clone the target came from
  # covers a tree that has since been collected; the object check covers a fetch
  # that could not reach it. A restore that silently checked out nothing would
  # leave the host exactly as dark as it already is.
  git fetch --quiet "${SOURCE_REPO}" "${recorded}" 2>/dev/null || true
  if ! git cat-file -e "${recorded}^{commit}" 2>/dev/null; then
    log "recorded generation ${recorded} is neither in ${DEPLOY_ROOT} nor fetchable from ${SOURCE_REPO}"
    return 1
  fi
  # The same fence the target passed above, for the same reason: an online roll
  # back across a protocol boundary is the thing the fence exists to refuse.
  target_protocol="$(git show "${recorded}:deploy/DEPLOY_PROTOCOL" 2>/dev/null || true)"
  if [[ "${target_protocol}" != "${DEPLOY_PROTOCOL}" ]]; then
    log "recorded generation ${recorded} implements '${target_protocol:-no protocol}', not ${DEPLOY_PROTOCOL}; refusing to cross a protocol boundary"
    return 1
  fi
  log "restoring the recorded generation ${recorded}"
  git reset --hard --quiet "${recorded}" || return 1
  AGENT_DEPLOYMENT_SHA="${recorded}"
  export AGENT_DEPLOYMENT_SHA
  DEFER_SHARED_READY_MARKER=1 bash "$SANDBOX_HOST_SCRIPT" up || return 1
  printf '%s\n' "${recorded}" > .deployed-sha || return 1
  bash "$SANDBOX_HOST_SCRIPT" mark-ready "${recorded}"
}

# Everything that makes this a deployment rather than a `git pull` lives in
# there, including the refusal to start runners on a host that cannot isolate
# them. It exits non-zero on a failed gateway probe, which is what turns a
# broken tunnel into a red deploy instead of jobs that claim and then fail.
if ! runs_phase activate; then
  log "handing over to sandbox_host.sh prepare"
  DEFER_SHARED_READY_MARKER=1 bash "$SANDBOX_HOST_SCRIPT" prepare
  log "phase ${PHASE} complete; this host still serves ${RECORDED:-nothing} and nothing was drained"
  exit 0
fi

# `up` when this is the whole rollout, `activate` when a deployer already ran
# `prepare` and put the control plane's database preparation between the two.
# The recovery below is the same either way: what it reacts to is a drained host
# advertising no generation, which only the second half can produce.
ROLLOUT_PHASE=up
runs_phase prepare || ROLLOUT_PHASE=activate
log "handing over to sandbox_host.sh ${ROLLOUT_PHASE}"
ROLLOUT_STATUS=0
DEFER_SHARED_READY_MARKER=1 bash "$SANDBOX_HOST_SCRIPT" "$ROLLOUT_PHASE" || ROLLOUT_STATUS=$?

if [ "${ROLLOUT_STATUS}" -ne 0 ]; then
  if advertises_a_generation; then
    die "the rollout of ${SHA} failed, but this host is still advertising the generation it was serving: nothing was interrupted and the queue is still being served"
  fi
  log "the rollout of ${SHA} failed after invalidating this host's marker: it advertises no deployment and will claim no work"
  if [ -z "${RECORDED}" ]; then
    die "the rollout of ${SHA} failed and .deployed-sha records no generation to fall back to. This host advertises no deployment and will claim no work until a rollout completes; roll back with a compatible commit."
  fi
  # Also the re-deploy case: a recorded generation equal to the target is the
  # same commit rolled out again, which is the cheapest possible recovery and
  # the one a deterministic failure will exhaust.
  if restore_recorded_generation "${RECORDED}"; then
    die "the rollout of ${SHA} failed and this host is serving ${RECORDED} again. The failure is still reported: this attempt did not pass its checks."
  fi
  die "the rollout of ${SHA} failed AND falling back to ${RECORDED} failed. This host advertises no deployment and will claim no work until a rollout completes."
fi

printf '%s\n' "${SHA}" > .deployed-sha
bash "$SANDBOX_HOST_SCRIPT" mark-ready "${SHA}"
log "deployed ${SHA} (was ${PREVIOUS})"
