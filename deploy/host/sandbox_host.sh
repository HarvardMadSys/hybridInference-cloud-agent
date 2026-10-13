#!/usr/bin/env bash
#
# Stand up agent-job runners on a machine that is NOT the gateway's.
#
# `agent_runner.sh` assumes the two are colocated: it layers the runner overlay
# on the main compose file, whose `agent-runner` declares `depends_on: backend`.
# Point that at a machine with no gateway and it does not fail — it starts one,
# on a box that was only ever meant to run jobs. This script uses a standalone
# compose file with no backend in it at all.
#
# Three narrow tunnel containers carry model, MCP, and control-plane traffic.
# Sandboxes run on one routable bridge per group, created by the runner and
# sandbox manager behind this deployment's host nftables table: public IPv4 on TCP
# 80/443, the named resolver and the two relays, nothing else. Setup and the
# agent share that bridge; there is no proxy and no network switch.
#
# Usage, from the repository root on the runner host:
#
#   deploy/host/sandbox_host.sh up       # build, start the tunnels, sandbox manager and runner
#   deploy/host/sandbox_host.sh status   # what is running, and the log tails
#   deploy/host/sandbox_host.sh check    # prove the tunnel reaches the gateway
#   deploy/host/sandbox_host.sh down     # stop everything this file started
#
# `up` in its two halves, for a deployer that has to put something between them
# — the release design sequences the control plane's database preparation
# between this host's preparation and its replacement (#270):
#
#   deploy/host/sandbox_host.sh prepare  # everything `up` does while the old
#                                        # generation is still serving
#   deploy/host/sandbox_host.sh activate # drain, replace, prove
#
# Required in the environment or in .env on this host:
#
#   COMPOSE_PROJECT                this stack's compose project, unique on this machine
#   AGENT_SANDBOX_IMAGE            image the sandboxes run
#   AGENT_TUNNEL_*                 inference tunnel destination/key/known_hosts
#   AGENT_MCP_TUNNEL_*             MCP-only tunnel destination/key/known_hosts
#   AGENT_CONTROL_PLANE_TUNNEL_*   control API tunnel destination/key/known_hosts
#   AGENT_SANDBOX_MANAGER_REVERSE_*         the control plane's route back to this sandbox manager
#   AGENT_SANDBOX_MANAGER_TOKEN   sandbox manager authentication secret
#   AGENT_EGRESS_CHAT_NETWORK_POOL this deployment's own RFC1918 pool for group bridges
#   AGENT_EGRESS_DNS_SERVER        the one IPv4 resolver sandboxes may reach
#
# Each tunnel key gets its own account on the far host, one authorized_keys
# line there, and that account's Match block from
# deploy/host/sshd-tunnels.conf.example; Admin → Servers → Server setup
# generates all of it. The inference key needs one forwarded port and no shell:
#
#   command="",restrict,port-forwarding,permitopen="127.0.0.1:8080" ssh-ed25519 AAAA... agent-tunnel
#
# The control-plane key also names the one reverse listener it may open:
#
#   command="",restrict,port-forwarding,permitopen="127.0.0.1:8000",permitlisten="172.24.0.1:18092" ssh-ed25519 AAAA... agent-control

set -Eeuo pipefail

AGENT_DEPLOY_MODE="${AGENT_DEPLOY_MODE:-source}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="deploy/host/docker-compose.sandbox.yml"
APP_DIR="${APP_DIR:-$(pwd)}"
cd "$APP_DIR"

# Same collision, same fix as the control plane's deploy.sh: the runners on
# this machine share this user's `$HOME`, so `~/.docker/buildx/current` is
# shared mutable state and a CI cell that finishes deletes the builder it put
# there — including one this bring-up is building through. A config directory
# under the deploy root keeps the `docker build` and `compose build` below on
# the daemon's own builder, which no other job can remove.
export BUILDX_CONFIG="${APP_DIR}/.buildx"

log() { printf '[sandbox-host] %s\n' "$*"; }
die() { printf '[sandbox-host] ERROR: %s\n' "$*" >&2; exit 1; }

case "$AGENT_DEPLOY_MODE" in
  source|artifacts) ;;
  *) die "AGENT_DEPLOY_MODE must be source or artifacts" ;;
esac

raw_env_or_dotenv() {
  # A value may come from the environment or from .env; compose reads both, so
  # the preflight has to look in both or it rejects working configurations.
  local name="$1"
  if [[ "$_COMPOSE_ENVIRONMENT_LOADED" == "1" ]]; then
    compose_env_value "$name"
    return 0
  fi
  if [[ "${!name+x}" == "x" ]]; then
    printf '%s' "${!name}"
    return 0
  fi
  # `.env` is optional — a host may configure everything in the environment.
  # The guard is load-bearing under `set -Eeuo pipefail`: without a file, sed
  # fails, pipefail propagates that out of the command substitution, and the
  # script dies at an assignment instead of taking the default. `require_env`
  # got away with it only because a failure inside `[[ ]]` is exempt from
  # `set -e`; that is luck, not a contract.
  [[ -f .env ]] || return 0
  local value
  value="$(sed -n "s/^${name}=//p" .env 2>/dev/null | tail -1)"
  # Compose strips one matched pair of surrounding quotes; a preflight that
  # does not would reject a `.env` compose accepts, and reject it by reporting
  # the variable as missing.
  value="${value%\"}"; value="${value#\"}"
  value="${value%\'}"; value="${value#\'}"
  printf '%s' "$value"
}

# Accept existing host .env files while examples and new deployments use the
# Sandbox Manager names. The compose wrapper also supplies the normalized token.
env_or_dotenv() {
  local name="$1" value legacy
  value="$(raw_env_or_dotenv "$name")"
  if [[ -n "$value" ]]; then
    printf '%s' "$value"
    return 0
  fi
  case "$name" in
    AGENT_SANDBOX_MANAGER_TOKEN) legacy=AGENT_WORKSPACE_BROKER_TOKEN ;;
    AGENT_SANDBOX_MANAGER_URL) legacy=AGENT_WORKSPACE_BROKER_URL ;;
    AGENT_SANDBOX_MANAGER_ADVERTISE_URL) legacy=AGENT_WORKSPACE_BROKER_ADVERTISE_URL ;;
    AGENT_SANDBOX_MANAGER_*) legacy="AGENT_BROKER_${name#AGENT_SANDBOX_MANAGER_}" ;;
    *) return 0 ;;
  esac
  raw_env_or_dotenv "$legacy"
}

# Ask Compose itself to parse the interpolation environment used by the stack.
# Reimplementing dotenv here is unsafe-by-disagreement: Compose resolves
# references, quotes and inline comments, and an exported empty value still
# outranks the file. A tiny stdin model avoids evaluating this stack's required
# substitutions before require_env can report them clearly.
_COMPOSE_ENVIRONMENT=""
_COMPOSE_ENVIRONMENT_LOADED=0
load_compose_environment() {
  [[ "$_COMPOSE_ENVIRONMENT_LOADED" == "0" ]] || return 0
  local command=(docker compose)
  [[ -f .env ]] && command+=(--env-file .env)
  if ! _COMPOSE_ENVIRONMENT="$(
    printf 'services: {}\n' | "${command[@]}" -f - config --environment
  )"; then
    die "docker compose could not resolve the environment used by this stack"
  fi
  _COMPOSE_ENVIRONMENT_LOADED=1
}

compose_env_value() {
  local name="$1" line
  while IFS= read -r line; do
    if [[ "${line%%=*}" == "$name" ]]; then
      printf '%s' "${line#*=}"
      return 0
    fi
  done <<< "$_COMPOSE_ENVIRONMENT"
}

# Every command that reads configuration, which is every command that is not
# pure help. Review found `prepare` and `activate` missing from this list after
# the phase split: they fell through to the hand-rolled dotenv reader below,
# which does not resolve `${VAR}` references or strip an inline comment. The
# same `.env` that `up` correctly refused was accepted by `prepare`, with a
# state root and a network name that were the literal text of the file.
case "${1:-}" in
  up|prepare|activate|check|status|down|mark-ready) load_compose_environment ;;
esac

if [[ "$AGENT_DEPLOY_MODE" == "artifacts" ]]; then
  source "$SCRIPT_DIR/../release/artifact_env.sh"
  prepare_artifact_deployment host "$_COMPOSE_ENVIRONMENT"
  _COMPOSE_ENVIRONMENT_LOADED=0
  load_compose_environment
fi

# Read through env_or_dotenv, and therefore only after it exists. Taking these
# from the shell environment alone silently ignored `.env` — the file this
# script's own header tells you to configure — so `up` built and ran the
# default image while `require_env`, which *does* read `.env`, reported the
# configuration complete. The two disagreed, and the one that acted was wrong.
# `.env` is the whole configuration surface here. The source repo also layered
# per-deployment env files from `distributions/`, which is that repository's
# overlay mechanism and does not exist in this one.
#
# **COMPOSE_PROJECT is required and has no default.** It prefixes every
# container, image and network this stack creates, so it is what keeps two
# deployments on one machine apart — and the failure when they share it is
# silent rather than loud: the second `up` does not refuse, it adopts the
# first's containers and recreates them with its own configuration. Staging and
# production share the runner host, so that is the normal case here, not an
# exotic one. A default would be a value somebody forgets to override exactly
# once.
COMPOSE_PROJECT="$(env_or_dotenv COMPOSE_PROJECT)"

# A function rather than an array, so the check happens when a compose command
# actually runs. `preflight` and `check` answer questions about this machine and
# touch no stack; making them fail for a missing project name would be refusing
# to answer "can this host isolate a sandbox" because of how the containers are
# to be labelled.
compose() {
  # Nested :? fallbacks are evaluated eagerly by Compose. Normalize the legacy
  # token here, then let the recipe require the canonical credential as before.
  local manager_token
  manager_token="$(env_or_dotenv AGENT_SANDBOX_MANAGER_TOKEN)"
  if [[ "$AGENT_DEPLOY_MODE" == "artifacts" ]]; then
    case "$1" in
      build) die "artifact deployments cannot build images" ;;
      up) shift; set -- up --pull never "$@" ;;
      run) shift; set -- run --pull never "$@" ;;
    esac
  fi
  [[ -n "$COMPOSE_PROJECT" ]] || die \
    "COMPOSE_PROJECT is not set — export it or put it in .env (e.g. freeinference-cloud-agent-sandbox-staging). It is what keeps this stack apart from the other environment on this machine."
  if [[ -f .env ]]; then
    AGENT_SANDBOX_MANAGER_TOKEN="$manager_token" \
    docker compose \
      -p "$COMPOSE_PROJECT" \
      -f "$COMPOSE_FILE" \
      --env-file .env "$@"
  else
    # Compose already receives exported configuration from this process. Do not
    # pass a path that does not exist: env-only hosts are an explicitly supported
    # direct-operator path, even though the CD wrapper deliberately requires a
    # deploy-root .env so credentials survive non-interactive service restarts.
    AGENT_SANDBOX_MANAGER_TOKEN="$manager_token" \
    docker compose \
      -p "$COMPOSE_PROJECT" \
      -f "$COMPOSE_FILE" "$@"
  fi
}

SANDBOX_IMAGE="$(env_or_dotenv AGENT_SANDBOX_IMAGE)"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-freeinference-cloud-agent-job:latest}"
GATEWAY_HOSTNAME="$(env_or_dotenv AGENT_GATEWAY_HOSTNAME)"
GATEWAY_HOSTNAME="${GATEWAY_HOSTNAME:-agent-gateway}"
TUNNEL_PORT="$(env_or_dotenv AGENT_TUNNEL_LISTEN_PORT)"
TUNNEL_PORT="${TUNNEL_PORT:-8080}"
MCP_HOSTNAME="$(env_or_dotenv AGENT_MCP_HOSTNAME)"
MCP_HOSTNAME="${MCP_HOSTNAME:-agent-mcp}"
MCP_TUNNEL_PORT="$(env_or_dotenv AGENT_MCP_TUNNEL_LISTEN_PORT)"
MCP_TUNNEL_PORT="${MCP_TUNNEL_PORT:-8000}"
CONTROL_PLANE_HOSTNAME="$(env_or_dotenv AGENT_CONTROL_PLANE_HOSTNAME)"
CONTROL_PLANE_HOSTNAME="${CONTROL_PLANE_HOSTNAME:-agent-control-plane}"
# Where the two relays are discovered: a route-less network no sandbox joins.
# Derived from the Compose project, so two stacks on one machine never find
# each other's relays; an explicit value still wins. The runner creates group
# bridges through the Docker API, outside Compose, so it must receive the exact
# daemon-side name Compose creates.
RELAY_NETWORK="$(env_or_dotenv AGENT_RELAY_NETWORK)"
RELAY_NETWORK="${RELAY_NETWORK:-${COMPOSE_PROJECT:+${COMPOSE_PROJECT}-}agent-relays}"
AGENT_RELAY_NETWORK="$RELAY_NETWORK"
export AGENT_RELAY_NETWORK

# Extra versions the image also carries, comma-separated, so an admin can switch
# new chats between them from the admin page without a rebuild. The image's
# default versions are protocol pins shared by the host and control plane, so
# they change in source together with `SHARED_SANDBOX_RUNTIME_PINS`, not through
# one host's environment. Empty is the normal case and means the image holds
# exactly those reviewed defaults.
#
# Build time decides the menu; the admin page decides which item is active. The
# control plane cannot offer a version this image does not contain, so what goes
# here is the ceiling on what an admin can pick.
CLAUDE_CODE_EXTRA_VERSIONS="$(env_or_dotenv CLAUDE_CODE_EXTRA_VERSIONS)"
CODEX_EXTRA_VERSIONS="$(env_or_dotenv CODEX_EXTRA_VERSIONS)"
PI_EXTRA_VERSIONS="$(env_or_dotenv PI_EXTRA_VERSIONS)"
OPENCODE_EXTRA_VERSIONS="$(env_or_dotenv OPENCODE_EXTRA_VERSIONS)"
KILO_EXTRA_VERSIONS="$(env_or_dotenv KILO_EXTRA_VERSIONS)"

require_env() {
  # Named up front, because each of these fails much later and much less
  # legibly: a missing token as a 401 per claim, a missing key as a container
  # that restarts forever, a missing known_hosts as a refusal nobody reads.
  local missing=() name
  for name in AGENT_SANDBOX_IMAGE \
              AGENT_SANDBOX_MANAGER_TOKEN \
              AGENT_TUNNEL_SSH_DESTINATION AGENT_TUNNEL_SSH_KEY \
              AGENT_TUNNEL_SSH_KNOWN_HOSTS \
              AGENT_MCP_TUNNEL_SSH_DESTINATION AGENT_MCP_TUNNEL_SSH_KEY \
              AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS \
              AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION \
              AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY \
              AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS \
              AGENT_SANDBOX_MANAGER_REVERSE_BIND AGENT_SANDBOX_MANAGER_REVERSE_PORT \
              AGENT_SANDBOX_MANAGER_ADVERTISE_URL \
              AGENT_EGRESS_CHAT_NETWORK_POOL AGENT_EGRESS_DNS_SERVER; do
    [[ -n "$(env_or_dotenv "$name")" ]] || missing+=("$name")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "missing from the environment or .env: ${missing[*]} (see ENVIRONMENT.md)"
  fi

  # On a machine running two environments, the enrollment-state root must be
  # said out loud. It is the only host path bind-mounted into the runner stack.
  # Chat files live exclusively in each sandbox container's Docker writable
  # layer; there is no host workspace or snapshot bind mount.
  #
  #   AGENT_HOST_STATE_ROOT   the enrolment credential. The control plane reads
  #                           the host *from the credential*, not from the claim
  #                           body, so the second stack registers as the first
  #                           one — `AGENT_RUNNER_HOST` cannot correct it.
  # Required only when a sibling is named, because that is exactly when the
  # enrollment default stops being safe.
  if [[ -n "$(env_or_dotenv SIBLING_COMPOSE_PROJECTS)" ]]; then
    local shared=() path unsafe
    for name in AGENT_HOST_STATE_ROOT; do
      path="$(env_or_dotenv "$name")"
      unsafe="$(python3 -c 'import os; print(os.path.realpath("/var/lib/hybridinference/agent-host"))')"
      if [[ -n "$path" ]]; then
        path="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$path")"
      fi
      if [[ -z "$path" || "$path" == "$unsafe" ]]; then
        shared+=("$name")
      fi
    done
    if [[ ${#shared[@]} -gt 0 ]]; then
      die "this host runs another environment (SIBLING_COMPOSE_PROJECTS is set), so ${shared[*]} must be unique to this stack; unset values and legacy shared defaults mix enrollment credentials across environments."
    fi
  fi

  local prefix key known
  for prefix in AGENT_TUNNEL AGENT_MCP_TUNNEL AGENT_CONTROL_PLANE_TUNNEL; do
    key="$(env_or_dotenv "${prefix}_SSH_KEY")"
    known="$(env_or_dotenv "${prefix}_SSH_KNOWN_HOSTS")"
    [[ -r "$key" ]] || die "${prefix}_SSH_KEY=$key is not readable on this host"
    [[ -s "$known" ]] || die \
      "${prefix}_SSH_KNOWN_HOSTS=$known is missing or empty. Generate it with
       ssh-keyscan, verify the far host's fingerprint, and mount it read-only;
       host verification is mandatory for every scoped relay."
  done

  # This compose file is the standalone/remote-host topology: its sandbox manager has no
  # published host port and shares no Docker network with the control-plane
  # stack. The reverse forward is therefore not an optional optimisation. A
  # missing half would let the host attest locally, then make the control deploy
  # wait the full readiness timeout on an address it can never reach.
  local reverse_bind reverse_port advertise_url
  reverse_bind="$(env_or_dotenv AGENT_SANDBOX_MANAGER_REVERSE_BIND)"
  reverse_port="$(env_or_dotenv AGENT_SANDBOX_MANAGER_REVERSE_PORT)"
  advertise_url="$(env_or_dotenv AGENT_SANDBOX_MANAGER_ADVERTISE_URL)"
  case "$reverse_bind" in
    0.0.0.0|127.0.0.1|localhost|::|::1)
      die "AGENT_SANDBOX_MANAGER_REVERSE_BIND=$reverse_bind is not the control-plane Docker bridge gateway: loopback is invisible to its containers and a wildcard exposes the sandbox manager to the host network"
      ;;
  esac
  [[ "$reverse_port" =~ ^[0-9]+$ \
     && "$reverse_port" -ge 1 \
     && "$reverse_port" -le 65535 ]] \
    || die "AGENT_SANDBOX_MANAGER_REVERSE_PORT must be an integer from 1 through 65535 (got '$reverse_port')"
  [[ "$advertise_url" =~ ^https?://[^/]+(:[0-9]+)?(/.*)?$ ]] \
    || die "AGENT_SANDBOX_MANAGER_ADVERTISE_URL must be an http(s) URL reachable by the control plane (got '$advertise_url')"
}

normalise_host_path() {
  local value="$1"
  while [[ "$value" != "/" && "$value" == */ ]]; do
    value="${value%/}"
  done
  printf '%s' "$value"
}

validate_retained_config() {
  # The control plane reconciles retained Docker state through authenticated
  # host heartbeats. Check the host-side credential before spending time on an
  # image build; the runner performs the authoritative token exchange later.
  local state_root enroll_token
  state_root="$(env_or_dotenv AGENT_HOST_STATE_ROOT)"
  state_root="${state_root:-/var/lib/hybridinference/agent-host}"
  enroll_token="$(env_or_dotenv AGENT_ENROLL_TOKEN)"
  if [[ -z "$enroll_token" && ! -s "${state_root%/}/credential" ]]; then
    die "retained sandbox mode requires AGENT_ENROLL_TOKEN or the persisted host credential at ${state_root%/}/credential"
  fi

  # The image defaults and SHARED_SANDBOX_RUNTIME_PINS are one reviewed
  # protocol unit. A per-host default override used to build successfully and
  # then make every runner omit shared-sandbox-v1, because the control plane
  # quite correctly rejected the mismatched attestation. Fail at configuration
  # time instead. A staging/rollback version is added to *_EXTRA_VERSIONS and
  # selected in Admin; a new default changes both source pin locations.
  local default_pin configured_pin
  for default_pin in CLAUDE_CODE_VERSION CODEX_VERSION PI_VERSION OPENCODE_VERSION KILO_VERSION; do
    configured_pin="$(env_or_dotenv "$default_pin")"
    [[ -z "$configured_pin" ]] || die \
      "$default_pin is a source-level shared protocol pin, not a host override. Remove it and put ${default_pin%_VERSION}_EXTRA_VERSIONS=$configured_pin in this host's configuration, then select that version in Admin; or update the Dockerfile and SHARED_SANDBOX_RUNTIME_PINS together in a reviewed change."
  done

  # One spelling, checked once. The runner normalises this value too, and its
  # own vocabulary is wider than the script's; a value only one side recognises
  # is how a rollout log comes to say "storage verified" for a host whose page
  # says the metric is switched off. Refusing it here means neither side has to
  # guess what the other made of it.
  local storage_probe
  storage_probe="$(storage_probe_name)"
  [[ "$storage_probe" == "overlay_root" || "$storage_probe" == "off" ]] \
    || die "AGENT_HOST_STORAGE_PROBE must be 'overlay_root' or 'off' (got '$storage_probe')"

  local slots resident_capacity chat_idle_timeout source_import_max
  # The limit this host serves until an admin sets one. The Servers page sets
  # size afterwards; this only has to be a valid starting point. Two is the
  # shared-live floor and 128 the pool's own ceiling.
  slots="$(env_or_dotenv AGENT_HOST_SLOTS)"
  slots="${slots:-3}"
  [[ "$slots" =~ ^[0-9]+$ && "$slots" -ge 2 && "$slots" -le 128 ]] \
    || die "AGENT_HOST_SLOTS must be an integer from 2 through 128 (got '${slots:-unset}')"
  if [[ -n "$(env_or_dotenv AGENT_RUNNER_REPLICAS)" ]]; then
    warn "AGENT_RUNNER_REPLICAS is no longer read: this host takes its concurrency limit from the admin Servers page and AGENT_HOST_SLOTS is only the default it starts with. Remove it from .env."
  fi
  resident_capacity="$(env_or_dotenv AGENT_SHARED_RESIDENT_CAPACITY)"
  resident_capacity="${resident_capacity:-8}"
  [[ "$resident_capacity" =~ ^[0-9]+$ \
     && "$resident_capacity" -ge 2 \
     && "$resident_capacity" -le 128 ]] \
    || die "AGENT_SHARED_RESIDENT_CAPACITY must be an integer from 2 through 128 (got '$resident_capacity')"
  chat_idle_timeout="$(env_or_dotenv AGENT_CHAT_IDLE_TIMEOUT_S)"
  chat_idle_timeout="${chat_idle_timeout:-1800}"
  [[ "$chat_idle_timeout" =~ ^[0-9]+([.][0-9]+)?$ \
     ]] && awk -v value="$chat_idle_timeout" 'BEGIN { exit !(value > 0) }' \
    || die "AGENT_CHAT_IDLE_TIMEOUT_S must be a positive number of seconds (got '$chat_idle_timeout')"
  # The runner would fall back to 1024 on anything else; refusing here means
  # an operator who raised it is not left on the default without noticing.
  # Base 10 and at most five significant digits, so neither a leading zero
  # (octal to Bash) nor an overlong value (wraps) decides differently from the
  # runner's int().
  source_import_max="$(env_or_dotenv AGENT_SOURCE_IMPORT_MAX_MIB)"
  source_import_max="${source_import_max:-1024}"
  [[ "$source_import_max" =~ ^0*[1-9][0-9]{0,4}$ ]] \
    && (( 10#$source_import_max <= 65536 )) \
    || die "AGENT_SOURCE_IMPORT_MAX_MIB must be a whole number of MiB from 1 through 65536 (got '$source_import_max')"

  # Docker DNS returns every container that claims an alias. If two relays use
  # one name, requests are round-robined to services that listen on different
  # ports: a readiness probe can win the lottery and a real job can lose it.
  # All three aliases share the runner's internal network, and the first two
  # also share the sandbox network, so pairwise uniqueness is mandatory.
  if [[ "$GATEWAY_HOSTNAME" == "$MCP_HOSTNAME" \
     || "$GATEWAY_HOSTNAME" == "$CONTROL_PLANE_HOSTNAME" \
     || "$MCP_HOSTNAME" == "$CONTROL_PLANE_HOSTNAME" ]]; then
    die "AGENT_GATEWAY_HOSTNAME ('$GATEWAY_HOSTNAME'), AGENT_MCP_HOSTNAME ('$MCP_HOSTNAME'), and AGENT_CONTROL_PLANE_HOSTNAME ('$CONTROL_PLANE_HOSTNAME') must be distinct Docker aliases"
  fi
  # Chat files live in Docker's writable layer. Verify the daemon whose socket
  # the sandbox manager receives is the daemon backed by the configured storage root;
  # otherwise the stack can come up green while retaining state on the wrong
  # disk. Whether that root is a durable mount remains host provisioning's job.
  local expected_root actual_root
  expected_root="$(env_or_dotenv AGENT_RETAINED_DOCKER_DATA_ROOT)"
  expected_root="$(normalise_host_path "${expected_root:-/var/lib/docker}")"
  [[ "$expected_root" == /* ]] \
    || die "AGENT_RETAINED_DOCKER_DATA_ROOT must be an absolute host path (got '$expected_root')"
  actual_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null)" \
    || die "could not inspect DockerRootDir for retained sandbox storage"
  actual_root="$(normalise_host_path "$actual_root")"
  [[ "$actual_root" == "$expected_root" ]] \
    || die "DockerRootDir is '${actual_root:-unknown}', but AGENT_RETAINED_DOCKER_DATA_ROOT expects '$expected_root'"

  log "retained storage verified at ${actual_root}; enrolled host state is available"
}

# The topology is unconditional, but a rolling deployment still needs to prove
# which code generation is online before the control plane replaces its API.
# This marker is runner infrastructure metadata, not chat state and not a
# feature switch; chat workspaces remain exclusively in Docker writable layers.
shared_live_ready_file() {
  # The deployment user owns this checkout, but Docker may own the enrollment
  # directory. Keeping this beside .deployed-sha avoids requiring write access
  # to credentials just to publish deployment readiness. Compose resolves its
  # ../../.shared-live-ready source to this same checkout root.
  printf '%s/.shared-live-ready' "${PWD%/}"
}

write_shared_live_marker() {
  local value="$1" ready_file ready_dir
  ready_file="$(shared_live_ready_file)"
  ready_dir="${ready_file%/*}"
  [[ "$ready_file" == /* && "$ready_dir" != "$ready_file" ]] \
    || die "shared-live ready marker must be an absolute file path (got '$ready_file')"
  mkdir -p "$ready_dir" \
    || die "could not create shared-live marker directory $ready_dir"
  # Keep the inode: every runner has a read-only file bind of this marker, so
  # replacing it by rename would leave running replicas reading the old inode.
  # While this overwrite is empty or partial, runners omit the exact-SHA
  # capability. That makes an interrupted write fail closed.
  printf '%s\n' "$value" > "$ready_file" \
    || die "could not write shared-live marker $ready_file"
  chmod 0644 "$ready_file" \
    || die "could not make shared-live marker readable by runner replicas"
}

prepare_shared_live_marker() {
  local deployment_sha
  deployment_sha="${AGENT_DEPLOYMENT_SHA:-}"
  if [[ -z "$deployment_sha" ]]; then
    deployment_sha="$(git rev-parse HEAD 2>/dev/null || true)"
  fi
  [[ -n "$deployment_sha" ]] \
    || die "shared-live deployment needs AGENT_DEPLOYMENT_SHA or a git checkout at the target commit"
  AGENT_DEPLOYMENT_SHA="$deployment_sha"
  export AGENT_DEPLOYMENT_SHA
  write_shared_live_marker "pending:${deployment_sha}"
  log "invalidated shared-live readiness until target ${deployment_sha} passes every host check"
}

complete_shared_live_marker() {
  local requested_sha="${1:-}" deployment_sha ready_file current
  deployment_sha="${requested_sha:-${AGENT_DEPLOYMENT_SHA:-}}"
  [[ -n "$deployment_sha" ]] || die "no deployment SHA was supplied for shared-live readiness"
  ready_file="$(shared_live_ready_file)"
  current=""
  if [[ -r "$ready_file" ]]; then
    current="$(<"$ready_file")"
  fi
  [[ "$current" == "pending:${deployment_sha}" ]] \
    || die "shared-live marker is '${current:-missing}', expected pending:${deployment_sha}; refusing to attest the wrong deployment"
  write_shared_live_marker "$deployment_sha"
  log "shared-live host deployment ${deployment_sha} is ready for control-plane activation"
}

# ── Storage probe attestation ─────────────────────────────────────────
#
# The admin page may say how full the filesystem holding sandbox writable
# layers is, but only about the right filesystem. The runner reads its own root
# filesystem — an overlay whose upper layer sits on that disk — and OverlayFS
# makes `statvfs("/")` describe the upper layer's filesystem. What it cannot
# tell is whether that filesystem is the disk or a project quota on it: Docker's
# `overlay2.size` (and a container's `--storage-opt size=`) is implemented as
# one, and a quota makes the container *and* `df <upperdir>` report the quota,
# with the same device number, so comparing the two proves nothing.
#
# The comparison that does prove something is against the **mount root** of the
# filesystem containing the upper directory, which no container directory's
# project covers. This section runs that comparison per replica and writes the
# result to a file the runner reads every sampling round.
#
# **Nothing here is fatal, and nothing here may hang.** A page that cannot say
# how full a disk is must not be able to stop the work that fills it, and a
# rollout waiting forever on a `docker exec` has stopped it just as effectively
# as a failed check. Every call that leaves this process therefore runs under
# `timeout`, with a bounded force-kill behind it, and each one may only spend
# what is left of the attestation's budget: a call that cannot be afforded is
# not made at all. When a call is cut short, or the budget runs out, the
# affected replicas are left with no entry — which the runner reads as
# `unverified` — and the deployment continues. The one exception is the verdict
# file itself, which is a bind-mount source: without it the runner containers
# cannot start at all, and failing there names the problem instead of letting
# Compose's own error do it.
#
# The honest limit, and it is a narrow one: a process in uninterruptible I/O
# cannot be killed by any signal, so `timeout` itself waits for it and no
# budget here can end that wait. Everything else is bounded — a wedged
# `docker exec`, a probe that ignores SIGTERM, a `df` on a dead mount that
# still obeys signals — and each call is charged to what is left rather than
# handed a fixed allowance.

# The diagnostic band's floor. Occupancy moves between the two reads — builds
# and cleanup interleave, and nothing pauses the world for monitoring — so
# `used` and `available` are compared within a tolerance and only as a
# diagnosis. `size` does not move under writes, and it is the actual test.
STORAGE_DIAGNOSTIC_FLOOR_BYTES=$((1024 * 1024 * 1024))

# Budgets. Each call here is local — a Python start-up in a sibling container,
# or `df` on a path — and returns in well under a second when it works, so
# these are orders of magnitude above the normal case. They exist for the case
# that does not work: a stuck `docker exec`, a `df` blocked on a dead mount.
#
# `STORAGE_CALL_TIMEOUT_S` is the SIGTERM point of one call, and
# `STORAGE_KILL_AFTER_S` the grace it then gets before SIGKILL. GNU `timeout`
# waits forever for a process that ignores TERM unless `-k` is given, so the
# pair is what makes "one call cannot outlive N seconds" true rather than
# nearly true. Both are charged to the attestation's budget, which is itself
# capped by whatever is left of the rollout's readiness fence — a host that
# publishes its marker after that fence expired has failed a deployment over a
# metric, so there is no point spending time on one.
STORAGE_CALL_TIMEOUT_S=8
STORAGE_KILL_AFTER_S=2
STORAGE_ATTEST_BUDGET_S=30

storage_bounded() {
  # Run one probe call under the smaller of the per-call bound and what is left
  # of the attestation's budget — the grace reserved for the force-kill, so a
  # call that has to be killed still fits inside the deadline. Returns 1
  # without running anything when the budget cannot afford a call at all,
  # which is how a caller skips work instead of starting it into a deadline it
  # has already passed.
  #
  # `-k` is not decoration: GNU `timeout` sends SIGTERM and then waits, so a
  # process that ignores it runs as long as it likes. The tests cover exactly
  # that process.
  local deadline="$1"
  shift
  local remaining=$(( deadline - SECONDS - STORAGE_KILL_AFTER_S ))
  if (( remaining <= 0 )); then
    return 1
  fi
  if (( remaining > STORAGE_CALL_TIMEOUT_S )); then
    remaining="$STORAGE_CALL_TIMEOUT_S"
  fi
  timeout -k "$STORAGE_KILL_AFTER_S" "$remaining" "$@"
}

storage_verdict_file() {
  # The deployment owns this dedicated directory. Runners bind the directory
  # read-only so they observe deletion/recreation of its verdict, without
  # gaining a view of the checkout, credentials or other host state.
  printf '%s/.storage-probe/verdict.json' "${PWD%/}"
}

storage_host_path() {
  # A mountinfo value escapes space, tab, newline, backslash and comma, and the
  # runner keeps its upper directory in that escaped form on purpose: the
  # string is the key its verdict is looked up by, so unescaping one side of
  # that lookup would break it. Host tools want the real bytes — `findmnt -T`,
  # `df` and `docker --mount src=` each stat the path they are handed, and a
  # literal `\040` names nothing — so the translation happens here, at that
  # boundary, and the key is never translated at all.
  #
  # Every backslash in such a value opens a three-digit octal escape, so
  # turning each one into `\0NNN` is precisely what `%b` reads back.
  local escaped="$1"
  printf '%b' "${escaped//\\/\\0}"
}

storage_probe_name() {
  # The deployment declares the topology; the collector never guesses it. An
  # absent value takes the same default the compose file interpolates.
  local probe
  probe="$(env_or_dotenv AGENT_HOST_STORAGE_PROBE)"
  printf '%s' "${probe:-overlay_root}"
}

write_storage_verdict() {
  # Replace the verdict document with the entries listed in "$1".
  #
  # Written beside the document and renamed over it, never truncated in place.
  # Runners bind the *directory* and open its current entry on every sample, so
  # a rename is seen — while a truncating write leaves whoever samples during
  # it holding an empty or half-written document. That reader has only one
  # conclusion available, "unverified", which is a host that has just passed
  # its attestation reported as unmeasured until its next cycle.
  #
  # Warns and returns non-zero rather than dying: both callers decide for
  # themselves, because one of them is preparing a mount source and the other
  # is refreshing a metric.
  local entries_file="$1" verdict_file
  verdict_file="$(storage_verdict_file)"
  if ! mkdir -p "${verdict_file%/*}" || ! chmod 0755 "${verdict_file%/*}"; then
    warn "could not prepare the storage-probe directory ${verdict_file%/*}; storage attestation not refreshed"
    return 1
  fi
  if ! python3 - "$entries_file" "$verdict_file" "${AGENT_DEPLOYMENT_SHA:-}" <<'PY'
import datetime
import json
import os
import sys

entries_file, verdict_file, deployment_sha = sys.argv[1:4]
entries = {}
with open(entries_file, encoding="utf-8") as handle:
    for line in handle:
        key, _, rest = line.rstrip("\n").partition("\t")
        verdict, _, rest = rest.partition("\t")
        mount_root, _, rest = rest.partition("\t")
        fstype, _, rest = rest.partition("\t")
        size, _, reason = rest.partition("\t")
        if not key or not verdict:
            continue
        entries[key] = {
            "verdict": verdict,
            "mount_root": mount_root or None,
            "fstype": fstype or None,
            "size_bytes": int(size) if size.isdigit() else None,
            "reason": reason or None,
        }

written_at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
document = {
    "schema_version": 1,
    "written_at": written_at,
    "deployment_sha": deployment_sha,
    "entries": entries,
}
# Same directory, so the rename is atomic: a replica sampling at any instant
# opens either the previous document or this one, and never a partial file.
# The mode is set before the rename for the same reason -- the document must
# not be readable-by-nobody for even the moment between the two.
temporary = verdict_file + ".tmp"
with open(temporary, "w", encoding="utf-8") as handle:
    json.dump(document, handle, sort_keys=True)
    handle.write("\n")
os.chmod(temporary, 0o644)
os.replace(temporary, verdict_file)
PY
  then
    rm -f "${verdict_file}.tmp"
    warn "could not write the storage verdict to ${verdict_file}; storage attestation not refreshed"
    return 1
  fi
  log "storage verdict written to ${verdict_file} ($(wc -l <"$entries_file" | tr -d '[:space:]') entries)"
}

prepare_storage_verdict() {
  # Keep the bound directory itself in place across up/check. The file inside
  # it can be deleted or replaced: every sample opens its current directory
  # entry, rather than an inode pinned by a file bind.
  # Fail-closed on purpose — a runner that starts while the checks are still
  # running reads "unverified", never the previous deployment's verdict. This
  # one dies where the refresh only warns: the directory is a mount source, and
  # without it the runner containers cannot start at all.
  write_storage_verdict /dev/null \
    || die "could not write the storage-probe verdict to $(storage_verdict_file)"
}

storage_quota_configured() {
  # Best effort, and known to be: `df` cannot distinguish a project quota on
  # the mount root itself from the disk, so the configuration is checked as
  # well. `overlay2.size` is the only quota this deployment can impose.
  #
  # The bracket in the pattern is load-bearing: `ps` snapshots the grep process
  # too, and a pattern spelled with a literal dot would match its own command
  # line and report a quota on every host.
  #
  # The file read is left unbounded on purpose — it is a local read with no
  # process to signal — while `ps`, which is a process like any other, is not.
  local deadline="${1:-$SECONDS}" daemon_json="${DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
  if [[ -r "$daemon_json" ]] && grep -q 'overlay2[.]size' "$daemon_json" 2>/dev/null; then
    return 0
  fi
  if storage_bounded "$deadline" ps -eo args= 2>/dev/null | grep -q -- 'overlay2[.]size'; then
    return 0
  fi
  return 1
}

storage_df_size() {
  # Bytes of the filesystem containing the path in `$1`, as GNU df reports it.
  # `-B1` with `--output=size` prints one exact integer: no human rounding, and
  # no dependence on the terminal's idea of a block. Prints nothing when df
  # cannot answer — including when it ran out of time or budget — so the caller
  # can tell "no answer" from a real zero.
  #
  # Bounded rather than optional: this runs inside the rollout, and an
  # unbounded `df` is how a monitoring check becomes a hung deployment.
  local value
  value="$(
    storage_bounded "$2" df -B1 --output=size "$1" 2>/dev/null \
      | tail -n1 | tr -d '[:space:]' || true
  )"
  if [[ ! "$value" =~ ^[0-9]+$ ]]; then
    return 0
  fi
  printf '%s' "$value"
}

storage_describe_fields() {
  # One separator-delimited line for a describe document: ok, reason, upperdir,
  # fstype, total, free, available.
  #
  # The separator is ASCII unit separator (0x1f), not a tab, and that is not
  # decoration: a tab is IFS *whitespace*, so `read` collapses runs of it and
  # an empty field — a null reason, say — would silently shift every field
  # after it. A non-whitespace separator preserves empty fields, which matters
  # because the reader below indexes them by position.
  python3 - "$1" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
        document = json.load(handle)
except (OSError, ValueError):
    document = {}
if not isinstance(document, dict):
    document = {}


def text(name):
    value = document.get(name)
    return "" if value is None else str(value)


print(
    "\x1f".join(
        [
            "1" if document.get("ok") else "0",
            text("reason"),
            text("upperdir"),
            text("fstype"),
            text("total_bytes") or "0",
            text("free_bytes") or "0",
            text("available_bytes") or "0",
        ]
    )
)
PY
}

storage_host_helper() {
  # Docker can resolve source directories that the deployment user cannot
  # traverse. Select a candidate mount from the *table*, without stat'ing the
  # path; the helper must independently prove the two actual binds match it.
  # Only those two directories are bound, non-recursively and read-only. No
  # host proc tree, capabilities, socket, credentials or network are passed.
  #
  # `bind-recursive=disabled` is the only spelling that works, and the choice
  # is not free. Both keys set the same field, but each is refused by half the
  # versions: the CLI parses this one and added it in 25, while the older
  # `bind-nonrecursive=true` is not merely deprecated on a current CLI — 29.2.1
  # rejects the whole `--mount` with "use bind-recursive=disabled instead" and
  # exits 125. Trying to reach older hosts with the old key therefore breaks
  # every host that is not old, which is all of them; this deployment needs
  # Docker 25 or newer for the helper path, and `docs/operations.md` says so.
  # A host below that gets `unverified` and a warning quoting the CLI's own
  # complaint, which is the honest outcome. Nothing here requires root: the
  # helper opens its two binds with `O_PATH`, which needs no permission on the
  # object, and answers with `fstatvfs`, so it runs as `nobody` and cannot read
  # a host file that merely happens to share the bound filesystem.
  local id="$1" upperdir="$2" deadline="$3" image helper_name helper_seconds rc=0
  local plan_file target fstype device fsroot upper_root
  plan_file="$4/${id}.mount-plan.json"
  if ! storage_bounded "$deadline" findmnt --kernel --list --json \
      --output TARGET,FSTYPE,MAJ:MIN,FSROOT >"$plan_file" 2>/dev/null; then
    rm -f "$plan_file"
    return 1
  fi
  target=""; fstype=""; device=""; fsroot=""; upper_root=""
  IFS=$'\037' read -r target fstype device fsroot upper_root < <(
    python3 - "$plan_file" "$upperdir" <<'PY_PLAN'
import json
import posixpath
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        mounts = json.load(handle)["filesystems"]
    path = sys.argv[2]
    if not path.startswith("/") or posixpath.normpath(path) != path:
        raise ValueError("non-canonical upperdir")
    candidates = [m for m in mounts if path == m["target"] or path.startswith(m["target"].rstrip("/") + "/")]
    # Later wins among equal-length targets: findmnt lists the mount table in
    # creation order, so an over-mounted path appears twice and the second
    # entry is the one a bind of that path actually resolves to. Taking the
    # first would hand the helper a plan for the mount it cannot reach, and
    # its identity check would then reject a sound host on every run.
    mount = max(reversed(candidates), key=lambda m: len(m["target"]))
    target, root = mount["target"], mount["fsroot"]
    upper_root = posixpath.join(root, posixpath.relpath(path, target))
    fields = [target, mount["fstype"], mount["maj:min"], root, posixpath.normpath(upper_root)]
    if not all(isinstance(v, str) and v and not any(ord(c) < 32 or c == "," for c in v)
               for v in [*fields, path]):
        raise ValueError("unsafe mount path")
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit(0)
print("\x1f".join(fields))
PY_PLAN
  ) || true
  rm -f "$plan_file"
  [[ -n "$target" && "$target" == /* && -n "$upper_root" ]] || return 1
  image="$(storage_bounded "$deadline" docker inspect --format '{{.Image}}' "$id" 2>/dev/null)" || return 1
  [[ "$image" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
  helper_seconds=$(( deadline - SECONDS - STORAGE_KILL_AFTER_S - 1 ))
  (( helper_seconds > 0 )) || return 1
  if (( helper_seconds > 5 )); then helper_seconds=5; fi
  helper_name="agent-storage-attest-${BASHPID:-$$}-${RANDOM}"
  storage_bounded "$deadline" docker run --rm --pull=never --name "$helper_name" \
    --label org.freeinference.storage-attestation=true \
    --runtime runc --network none --read-only --user 65534:65534 \
    --cap-drop ALL --security-opt no-new-privileges:true \
    --pids-limit 16 --memory 64m --cpus 0.25 \
    --mount "type=bind,src=${target},dst=/disk,readonly,bind-recursive=disabled,bind-propagation=rslave" \
    --mount "type=bind,src=${upperdir},dst=/upper,readonly,bind-recursive=disabled,bind-propagation=rslave" \
    --env PYTHONDONTWRITEBYTECODE=1 --entrypoint python "$image" \
    -m cloud_agent_host.storage_attestation "$target" "$device" "$fsroot" "$upper_root" "$fstype" "$helper_seconds" || rc=$?
  if (( rc != 0 )); then
    # --rm handles completed helpers; explicit cleanup also covers create or
    # start failures. It may only use the same remaining attestation budget.
    storage_bounded "$deadline" docker rm -f "$helper_name" >/dev/null 2>&1 || true
  fi
  return "$rc"
}

storage_helper_fields() {
  # A malformed/partial helper response is absence, never zero capacity.
  python3 - "$1" <<'PY_HELPER'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        value = json.load(handle)
    fields = [value[name] for name in (
        "mount_root", "fstype", "total_bytes", "upper_total_bytes", "used_bytes", "available_bytes"
    )]
    if not all(isinstance(item, str) and item and not any(ord(c) < 32 for c in item)
               for item in fields[:2]) or not fields[0].startswith("/"):
        raise ValueError("invalid mount")
    if not all(type(item) is int and 0 <= item <= 2**53 - 1 for item in fields[2:]):
        raise ValueError("invalid counters")
    if fields[4] + fields[5] > fields[2]:
        raise ValueError("invalid occupancy")
except (OSError, ValueError, KeyError, TypeError):
    raise SystemExit(0)
print("\x1f".join(map(str, fields)))
PY_HELPER
}

storage_occupancy_fields() {
  # "used available" from a describe document, or nothing at all.
  #
  # Nothing rather than zeroes: a document that timed out, arrived empty or
  # cannot be parsed must not read as a filesystem with nothing on it, which is
  # a reading the comparison would happily accept.
  python3 - "$1" <<'PY_OCCUPANCY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        reading = json.load(handle)
except (OSError, ValueError):
    raise SystemExit(0)
if not isinstance(reading, dict):
    raise SystemExit(0)
size = reading.get("total_bytes")
free = reading.get("free_bytes")
available = reading.get("available_bytes")
if not all(isinstance(value, int) and not isinstance(value, bool)
           for value in (size, free, available)):
    raise SystemExit(0)
print(size - free, available)
PY_OCCUPANCY
}

storage_usage_agrees() {
  # Compare the container's occupancy figures with the host's, up to three
  # times, and print a note describing the last discrepancy. Returns 0 when the
  # two agree within max(1 GiB, 1% of the size).
  #
  # Used and available are diagnostics rather than a topology test: they move
  # between the two reads because builds and cleanup interleave, so requiring
  # equality would fail on any busy host. A monotonic assumption is not
  # available either — nothing here pauses the world for monitoring.
  #
  # Both calls are bounded, and the whole loop stops at the caller's deadline
  # instead of retrying into it: three attempts plus their pauses is the most
  # this can add to a rollout.
  local id="$1" mount_root="$2" size="$3" deadline="${4:-$SECONDS}" attempt tolerance pause
  local container_used container_avail host_used host_avail delta_used delta_avail
  local note="the reading could not be taken" container_line host_line
  local helper_upper="${5:-}" helper_file="${6:-}" helper_root helper_type helper_size helper_upper_size
  # The document the parallel phase already saved for this replica. Retries
  # overwrite it, so it always holds the most recent describe for this runner.
  local describe_file="$7"
  tolerance=$(( size / 100 ))
  if (( tolerance < STORAGE_DIAGNOSTIC_FLOOR_BYTES )); then
    tolerance="$STORAGE_DIAGNOSTIC_FLOOR_BYTES"
  fi
  for attempt in 1 2 3; do
    if (( deadline - SECONDS <= STORAGE_KILL_AFTER_S )); then
      note="the attestation ran out of its budget before the occupancy could be compared"
      break
    fi
    if (( attempt == 1 )) && [[ -s "$describe_file" ]]; then
      # The parallel phase ran this exact command moments ago and kept what it
      # said. Asking again costs a second interpreter start inside the
      # container, charged to the same fence — and on a rollout with little
      # left, that is the call that cannot be afforded, which leaves the host
      # unverified over a reading already in hand. Seconds of drift are what
      # the tolerance below exists for.
      container_line="$(storage_occupancy_fields "$describe_file")"
    else
      storage_bounded "$deadline" docker exec "$id" \
        python -m cloud_agent_host.host_capacity describe >"$describe_file" 2>/dev/null \
        || : >"$describe_file"
      container_line="$(storage_occupancy_fields "$describe_file")"
    fi
    if [[ -n "$helper_upper" ]]; then
      # Refresh the independent host reading on retries as occupancy moves.
      if (( attempt > 1 )); then
        storage_host_helper "$id" "$helper_upper" "$deadline" "${helper_file%/*}" >"$helper_file" 2>/dev/null || : >"$helper_file"
      fi
      host_used=""; host_avail=""; helper_root=""; helper_size=""; helper_upper_size=""
      IFS=$'\037' read -r helper_root helper_type helper_size helper_upper_size host_used host_avail \
        < <(storage_helper_fields "$helper_file") || true
      host_line=""
      if [[ "$helper_root" == "$mount_root" && "$helper_size" == "$size" && "$helper_upper_size" == "$size" ]]; then
        host_line="$host_used $host_avail"
      fi
    else
      host_line="$(
        storage_bounded "$deadline" df -B1 --output=used,avail "$mount_root" 2>/dev/null \
          | tail -n1 || true
      )"
    fi
    # Cleared first: `read` leaves its targets alone at EOF, so a second
    # attempt would otherwise compare the previous attempt's numbers.
    container_used=""; container_avail=""; host_used=""; host_avail=""
    read -r container_used container_avail <<<"$container_line" || true
    read -r host_used host_avail <<<"$host_line" || true
    if [[ -z "$container_used" || -z "$container_avail" || -z "$host_used" || -z "$host_avail" ]]; then
      note="a reading did not arrive within ${STORAGE_CALL_TIMEOUT_S}s (the probe or df was cut short)"
    elif [[ ! "$container_used" =~ ^[0-9]+$ || ! "$container_avail" =~ ^[0-9]+$ \
       || ! "$host_used" =~ ^[0-9]+$ || ! "$host_avail" =~ ^[0-9]+$ ]]; then
      note="one of the two readings was not numeric"
    else
      delta_used=$(( container_used - host_used ))
      delta_avail=$(( container_avail - host_avail ))
      if (( delta_used < 0 )); then delta_used=$(( -delta_used )); fi
      if (( delta_avail < 0 )); then delta_avail=$(( -delta_avail )); fi
      note="used differs by ${delta_used} B and available by ${delta_avail} B (tolerance ${tolerance} B)"
      if (( delta_used <= tolerance && delta_avail <= tolerance )); then
        printf '%s' "$note"
        return 0
      fi
    fi
    if (( attempt < 3 )); then
      # The wait is inside the budget as well: a retry that cannot afford its
      # own pause has nothing left to spend on the attempt after it.
      pause=$(( deadline - SECONDS ))
      if (( pause > 2 )); then pause=2; fi
      if (( pause > 0 )); then sleep "$pause"; fi
    fi
  done
  printf '%s' "$note"
  return 1
}

append_storage_entry() {
  # Key, verdict, mount root, filesystem type, size and reason; the file last.
  local key="$1" verdict="$2" mount_root="$3" fstype="$4" size="$5" reason="$6" file="$7"
  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$key" "$verdict" "$mount_root" "$fstype" "$size" "$reason" >>"$file"
}

verify_storage_probe() {
  # Attest every replica's storage reading and write the verdict document.
  #
  # Takes the attestation directory, the runner ids, the readiness deadline and
  # whether that deadline is a fence, from check_shared_live_stack — which has
  # already proven the replicas are alive, because a verdict about a container
  # that is not running would be about nothing.
  #
  # The two kinds of caller need different budgets and the difference is not
  # cosmetic. A rollout's deadline is a promise to the control plane: whatever
  # the readiness probes have already spent is gone, and a fence that has
  # already expired means this attestation gets nothing at all — the marker is
  # late whether or not it runs. A manual `check` has no fence; its deadline is
  # "now", and it is the path the design gives operators to refresh a verdict,
  # so it gets the attestation's own budget.
  local attest_dir="$1" runner_ids="$2" deadline="${3:-$SECONDS}" fenced="${4:-0}"
  local entries_file id index storage_rc ok reason upperdir fstype
  local total upper_size root_size mount_root host_fstype host_upperdir helper_rc note storage_deadline
  local helper_upper helper_file helper_used helper_available
  local -a ids=() pids=()

  if [[ "$(storage_probe_name)" == "off" ]]; then
    # Clear the entries rather than leaving the previous verdict in place. A
    # replica started before the switch still carries `overlay_root` in its
    # environment — an environment is fixed when the container is created — and
    # against a stale verdict it would go on publishing capacity for a host the
    # operator has just switched off. Emptying the document stops the figures
    # now; the `up` that recreates the container stops the measuring.
    log "storage measurement is switched off; clearing the verdict so replicas from before the switch stop publishing capacity"
    : >"${attest_dir}/storage-entries.tsv"
    write_storage_verdict "${attest_dir}/storage-entries.tsv" || true
    return 0
  fi
  if [[ -z "$runner_ids" ]]; then
    warn "no runner replica is available to attest storage against"
    return 0
  fi
  # `timeout` is what makes the per-call bound real, and every call below is
  # local work with no business outliving a few seconds. Without it this host
  # gets no storage measurement at all, rather than an unbounded `docker exec`
  # in the rollout path: "no number" is the right answer when the only way to
  # get one is to risk holding a deployment open.
  if ! command -v timeout >/dev/null 2>&1; then
    warn "coreutils 'timeout' is not on PATH; skipping storage attestation rather than risking an unbounded probe call"
    return 0
  fi
  if (( fenced )); then
    storage_deadline="$deadline"
    if (( storage_deadline > SECONDS + STORAGE_ATTEST_BUDGET_S )); then
      storage_deadline=$((SECONDS + STORAGE_ATTEST_BUDGET_S))
    fi
  else
    storage_deadline=$((SECONDS + STORAGE_ATTEST_BUDGET_S))
  fi
  if (( storage_deadline - SECONDS <= STORAGE_KILL_AFTER_S )); then
    warn "the rollout's readiness fence has no room left for a storage attestation; leaving storage unverified"
    return 0
  fi

  # Asked in parallel and through the same command the runner samples with: a
  # shell re-implementation could agree with itself while both differ from
  # what the heartbeat will carry.
  for id in $runner_ids; do
    (
      storage_rc=0
      storage_bounded "$storage_deadline" docker exec "$id" \
        python -m cloud_agent_host.host_capacity describe \
        >"${attest_dir}/${id}.storage.json" 2>"${attest_dir}/${id}.storage.stderr" \
        || storage_rc=$?
      printf '%s' "${storage_rc}" >"${attest_dir}/${id}.storage.status"
    ) &
    ids+=("$id")
    pids+=("$!")
  done
  for index in "${!pids[@]}"; do
    wait "${pids[index]}" || true
  done

  entries_file="${attest_dir}/storage-entries.tsv"
  : >"$entries_file"
  for index in "${!ids[@]}"; do
    id="${ids[index]}"
    if (( storage_deadline - SECONDS <= STORAGE_KILL_AFTER_S )); then
      # No entry for the replicas that were not reached: the runner draws the
      # same conclusion from an absent entry, which is `unverified`.
      warn "the storage attestation ran out of its budget; the remaining replicas keep reporting unverified"
      break
    fi
    storage_rc="$(cat "${attest_dir}/${id}.storage.status" 2>/dev/null || true)"
    if [[ "$storage_rc" == "124" || "$storage_rc" == "137" ]]; then
      # 124 is timeout's own verdict; 137 is what it reports when the call had
      # to be SIGKILLed after ignoring SIGTERM.
      warn "runner $id did not answer the storage probe description within its budget; leaving its storage unverified"
      continue
    fi
    if [[ "$storage_rc" != "0" ]]; then
      warn "runner $id could not run the storage probe description; leaving its storage unverified"
      continue
    fi
    ok=""; reason=""; upperdir=""; fstype=""; total=""
    # The two free-space fields are deliberately dropped here: occupancy is
    # compared by storage_usage_agrees below, under a tolerance, and reading
    # them into names nobody reads is how a stricter check than the design's
    # gets added by accident.
    # shellcheck disable=SC2034
    IFS=$'\037' read -r ok reason upperdir fstype total ignored_free ignored_avail \
      < <(storage_describe_fields "${attest_dir}/${id}.storage.json") || true
    if [[ "$ok" != "1" || -z "$upperdir" ]]; then
      # No entry is written: the runner reaches the same conclusion on its own,
      # and reporting it "unsupported" here would speak for a probe that has
      # not been asked. A reason of `source_unavailable` may be transient.
      warn "runner $id cannot describe its storage filesystem (${reason:-no reason}); it will report no reading"
      continue
    fi
    # Every use below hands this path to a host tool, which wants the real
    # bytes. `$upperdir` keeps its escapes: it is the key the runner looks its
    # own verdict up by, and a key translated on one side is not a key.
    host_upperdir="$(storage_host_path "$upperdir")"

    # The mount root, not the upper directory: a project quota on the upper
    # directory's project returns the quota from both, so the two would agree
    # while neither describes the disk.
    root_size=""; upper_size=""; helper_upper=""; helper_rc=0
    mount_root=""; host_fstype=""
    helper_file="${attest_dir}/${id}.host-storage.json"
    # Both fields from one call, and the type first. Two separately bounded
    # calls are two chances to be cut short and two reads of a mount table that
    # moves, so the pair they return need not describe the same mount; and the
    # type never contains a blank, which leaves the rest of the line — blanks
    # and all — to the mount point.
    #
    # That type is the *host's*, not the container's: inside the container `/`
    # is an overlay, and the entry names the filesystem the writable layers
    # actually live on. When it cannot be established the entry says nothing,
    # rather than repeating the `overlay` every container would report.
    read -r host_fstype mount_root < <(
      storage_bounded "$storage_deadline" findmnt -n -o FSTYPE,TARGET -T "$host_upperdir" 2>/dev/null \
        | head -n1 || true
    ) || true
    if [[ -z "$mount_root" || "$mount_root" != /* ]]; then
      # The ordinary host-user path remains useful on accessible filesystems.
      # A permission failure needs independent metadata, not a guessed parent.
      if storage_host_helper "$id" "$host_upperdir" "$storage_deadline" "$attest_dir" >"$helper_file" \
          2>"${attest_dir}/${id}.host-storage.stderr"; then
        IFS=$'\037' read -r mount_root host_fstype root_size upper_size helper_used helper_available \
          < <(storage_helper_fields "$helper_file") || true
        if [[ -n "$root_size" ]]; then helper_upper="$host_upperdir"; fi
      else
        helper_rc=$?
      fi
    fi
    if [[ -z "$mount_root" || "$mount_root" != /* ]]; then
      if (( helper_rc != 0 )); then
        # Name the tool that failed. The helper exists exactly where findmnt
        # cannot answer, so reporting its failure as findmnt's sends the reader
        # to a permission problem that is not the one they have — and the
        # helper's own diagnosis is on a stream nothing else ever prints.
        append_storage_entry "$upperdir" "unverified" "" "$host_fstype" "$total" \
          "the read-only storage helper exited ${helper_rc} instead of locating the filesystem containing ${host_upperdir}" \
          "$entries_file"
        warn "runner $id: the read-only storage helper exited ${helper_rc}; leaving its storage unverified"
        if [[ -s "${attest_dir}/${id}.host-storage.stderr" ]]; then
          printf '[sandbox-host] %s storage helper said:\n' "$id" >&2
          tail -n 5 "${attest_dir}/${id}.host-storage.stderr" >&2 || true
        fi
      else
        append_storage_entry "$upperdir" "unverified" "" "$host_fstype" "$total" \
          "the host could not locate the filesystem containing ${host_upperdir}" "$entries_file"
        warn "runner $id: findmnt could not locate the filesystem containing ${host_upperdir}"
      fi
      continue
    fi
    if [[ -z "$helper_upper" ]]; then
      root_size="$(storage_df_size "$mount_root" "$storage_deadline")"
    fi
    if [[ -z "$root_size" ]]; then
      append_storage_entry "$upperdir" "unverified" "$mount_root" "$host_fstype" "$total" \
        "df could not size ${mount_root} on the host" "$entries_file"
      warn "runner $id: df could not size ${mount_root}"
      continue
    fi
    if storage_quota_configured "$storage_deadline"; then
      append_storage_entry "$upperdir" "unsupported" "$mount_root" "$host_fstype" "$root_size" \
        "the Docker daemon is configured with overlay2.size, so this reading is a project quota" \
        "$entries_file"
      warn "runner $id: daemon configuration sets overlay2.size; storage capacity would be a quota, not the disk"
      continue
    fi
    if [[ -z "$helper_upper" ]]; then
      upper_size="$(storage_df_size "$host_upperdir" "$storage_deadline")"
    fi
    if [[ -z "$upper_size" ]]; then
      # "No answer" is not "a different answer". A `df` that was cut short, or
      # skipped because the budget ran out, returns nothing — and comparing
      # that nothing against the mount root's size disagrees with it, which is
      # how a missing measurement becomes a statement about the host's
      # configuration that nobody established.
      append_storage_entry "$upperdir" "unverified" "$mount_root" "$host_fstype" "$root_size" \
        "df could not size ${host_upperdir} within the attestation's budget" "$entries_file"
      warn "runner $id: df could not size ${host_upperdir}; leaving its storage unverified"
      continue
    fi
    if [[ "$upper_size" != "$root_size" ]]; then
      append_storage_entry "$upperdir" "unsupported" "$mount_root" "$host_fstype" "$root_size" \
        "df reports ${upper_size} B for ${host_upperdir} but ${root_size} B for its mount root, which is a project quota" \
        "$entries_file"
      warn "runner $id: the upper directory and its mount root disagree on size; treating the topology as unsupported"
      continue
    fi
    if [[ "$total" != "$root_size" ]]; then
      # `size` is the hard criterion: it does not move under writes, so a
      # mismatch means the container is not looking at the filesystem the host
      # just measured.
      append_storage_entry "$upperdir" "unsupported" "$mount_root" "$host_fstype" "$root_size" \
        "the container reports ${total} B for its root filesystem but the host reports ${root_size} B for ${mount_root}" \
        "$entries_file"
      warn "runner $id: the container and the host disagree on the filesystem's size; treating the topology as unsupported"
      continue
    fi
    if note="$(storage_usage_agrees "$id" "$mount_root" "$root_size" "$storage_deadline" \
        "$helper_upper" "$helper_file" "${attest_dir}/${id}.storage.json")"; then
      append_storage_entry "$upperdir" "verified" "$mount_root" "$host_fstype" "$root_size" "" "$entries_file"
      log "runner $id: storage verified against ${mount_root} (${host_fstype:-unknown filesystem}, ${root_size} B)"
    else
      append_storage_entry "$upperdir" "unverified" "$mount_root" "$host_fstype" "$root_size" \
        "the container's occupancy never matched the host's: ${note}" "$entries_file"
      warn "runner $id: occupancy readings did not agree; ${note}"
    fi
  done

  write_storage_verdict "$entries_file" || return 0
}

probe_sandbox_relay() {
  local network="$1" url="$2" label="$3" deadline="$4" diagnostic="$5"
  local relay_service="$6" relay_id relay_ip relay_host
  local backend code="" first_failure=1
  local runtime_args=()
  backend="$(env_or_dotenv AGENT_SANDBOX_BACKEND)"
  backend="${backend:-kata}"
  if [[ "$backend" == "kata" ]]; then
    runtime_args=(--runtime "$KATA_RUNTIME")
  fi

  while true; do
    if [[ "$backend" == "kata" ]]; then
      # Docker's embedded resolver is not inside the Kata VM. Resolve only
      # this compose-owned relay's endpoint on the exact probe bridge.
      relay_id="$(compose ps -q "$relay_service")"
      relay_ip="$(docker inspect "$relay_id" | python3 -c \
        'import json, sys; print(json.load(sys.stdin)[0]["NetworkSettings"]["Networks"][sys.argv[1]]["IPAddress"])' "$network")"
      [[ -n "$relay_ip" ]] || die "${label} has no address on ${network}"
      relay_host="${url#*://}"
      relay_host="${relay_host%%:*}"
      runtime_args=(--runtime "$KATA_RUNTIME" --add-host "${relay_host}=${relay_ip}")
    fi
    # Pass the URL as an argv value rather than interpolating operator config
    # into shell source. `-f` matters: a tunnel that reaches a 503 is reachable
    # but not ready, and must not publish the exact-SHA capability.
    if code="$(docker run --rm --network "$network" \
         ${runtime_args[@]+"${runtime_args[@]}"} \
         --entrypoint /bin/sh "$SANDBOX_IMAGE" \
         -c 'curl -fsS -o /dev/null -w "%{http_code}" --max-time 15 "$1"' \
         sh "$url" 2>/dev/null)" && [[ "$code" == "200" ]]; then
      log "the sandbox network can reach the healthy ${label} through its tunnel"
      return 0
    fi
    if (( SECONDS >= deadline )); then
      die "a sandbox on ${network} cannot reach a healthy ${url} (last HTTP status: ${code:-none}). ${diagnostic}"
    fi
    if [[ "$first_failure" == "1" ]]; then
      log "${label} is not ready from ${network} yet; waiting for the parallel control deployment"
      first_failure=0
    fi
    sleep 3
  done
}

# Per-replica attestation output, created by check_shared_live_stack and
# removed by the trap that function registers. Deliberately not `local` to it:
# `local` is gone by the time the trap runs, and under `set -u` that turns a
# failed deployment's cleanup into a second, unrelated error.
ATTEST_DIR=""

cleanup_attest_dir() {
  if [[ -n "${ATTEST_DIR}" ]]; then
    rm -rf -- "${ATTEST_DIR}"
    ATTEST_DIR=""
  fi
}

wait_for_sandbox_manager_health() {
  # The runners' preflight asks the sandbox manager for its retained sandboxes, and it
  # asks exactly once. `compose up -d` returns as soon as the containers are
  # created, so on a rollout that replaced the sandbox manager the replicas were judging
  # one that had been alive for a second: long enough to accept a connection,
  # not long enough to have finished reconciling the sandboxes it serves. Every
  # replica then reported "chat Session Supervisor is unavailable" and the
  # rollout failed over a stack that was healthy a few seconds later.
  #
  # Waiting here is what taking the fixed five-second pause out of this path was
  # supposed to leave behind: a wait on the fact the next step depends on rather
  # than on a timer. A sandbox manager that is already serving costs one exec.
  local deadline="${1:-$SECONDS}" sandbox_manager_id
  sandbox_manager_id="$(compose ps -q sandbox-manager)" || sandbox_manager_id=""
  [[ -n "$sandbox_manager_id" ]] \
    || die "no sandbox-manager replica is running; shared-live cannot be attested"
  while :; do
    # Any HTTP answer settles it, including the 401 an unauthenticated probe
    # gets: this waits for the server to be serving, not for it to authorize.
    # A refused connection is the state worth waiting out.
    if docker exec "$sandbox_manager_id" python3 -c 'import urllib.error, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:8092/health", timeout=2)
except urllib.error.HTTPError:
    pass' >/dev/null 2>&1; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      die "the sandbox manager did not answer /health before the readiness deadline.
     'docker logs ${sandbox_manager_id}' shows why it is not serving; a reconcile that
     raises at startup exits the process and every runner then reports the chat
     session supervisor as unavailable."
    fi
    sleep 1
  done
}

check_shared_live_stack() {
  local deadline="${1:-$SECONDS}" fenced="${2:-0}"
  # Ask the runner container to complete the same executable preflight as its
  # turns' own processes, then attest the exact image labels, helper pins,
  # runtime drivers, sandbox manager settings and default pool size used by the claim
  # path.  Calling preflight here is intentional: a freshly started runner may
  # still be blocked inside its own kernel/host-rule/relay probe while
  # `docker exec` already works.  Static capability inspection during that
  # window would publish the ready marker immediately before the runner
  # crash-looped.
  local runner_ids id capability first_id="" turn_probe
  runner_ids="$(compose ps -q runner)" \
    || die "could not list the runner container for shared-live verification"
  [[ -n "$runner_ids" ]] || die "no running runner container is available to attest shared-sandbox-v1"

  # Only once the container that will be asked is known to be there: this is
  # about what it is about to ask the sandbox manager, so it must not pre-empt a more
  # specific verdict about the runner itself.
  wait_for_sandbox_manager_health "$deadline"

  # Every listed container attests at the same time — a rollout can briefly
  # list the old one beside the new. The preflight is dominated by the real
  # request it makes through the tunnels, not by anything they share, and none
  # reads another's answer, so a serial loop bought nothing and cost the sum
  # of the waits. Verdicts are still reported per container, in listing order,
  # so a failure names the container it came from and the first one is still
  # the one asked to start a turn process and dial the sandbox manager below.
  #
  # Each container's exit status goes to a file because `wait` is reaped in a
  # second pass, after every one has been started — and it is captured with
  # `|| rc=$?` rather than read from `$?` after the command: under `set -e` a
  # failing `docker exec` ends this subshell where it stands, so a status
  # written after it would never be written at all, on the only path that reads
  # it. The code is carried into the verdict because 125/126/127 mean the
  # container or the exec itself is gone, which is a different problem from a
  # preflight that raised.
  ATTEST_DIR="$(mktemp -d)" \
    || die "could not create a directory for per-container attestation output"
  # Registered here and not at the top of the script, because a trap belongs to
  # the shell that ran the function. `cmd_up` runs these checks in a subshell —
  # `( cmd_check ... ) || probe_ok=$?` — and a subshell resets traps to its
  # parent's, so a handler installed by the script never runs for an `exit`
  # inside it. Every failed `up` would leave its directory behind, and that is
  # the path that fails.
  trap cleanup_attest_dir EXIT
  local -a attest_ids=() attest_pids=()
  local index attest_status
  for id in $runner_ids; do
    [[ -n "$first_id" ]] || first_id="$id"
    (
      attest_rc=0
      docker exec "$id" python -c \
        'from cloud_agent_host.runner import _shared_live_host_capabilities as capabilities; from cloud_agent_host.sandbox import build_backend_from_env; backend = build_backend_from_env(); backend.preflight(); print(",".join(capabilities(backend, backend.version_stamp())))' \
        >"${ATTEST_DIR}/${id}.capability" 2>"${ATTEST_DIR}/${id}.stderr" || attest_rc=$?
      printf '%s' "${attest_rc}" >"${ATTEST_DIR}/${id}.status"
    ) &
    attest_ids+=("$id")
    attest_pids+=("$!")
  done
  for index in "${!attest_pids[@]}"; do
    wait "${attest_pids[index]}" || true
  done
  # Collect every replica's verdict before failing on any of them. All of them
  # have run by now, so all of their answers are available at once — which the
  # serial loop could never offer — and "all three failed" (the configuration)
  # is a different problem from "one failed" (that machine).
  local -a verdicts=()
  for index in "${!attest_ids[@]}"; do
    id="${attest_ids[index]}"
    attest_status=""
    if [[ -r "${ATTEST_DIR}/${id}.status" ]]; then
      attest_status="$(<"${ATTEST_DIR}/${id}.status")"
    fi
    if [[ "$attest_status" != "0" ]]; then
      verdicts+=("runner $id could not complete sandbox preflight and shared-live capability attestation (docker exec status: ${attest_status:-not written})")
      continue
    fi
    capability="$(<"${ATTEST_DIR}/${id}.capability")"
    case ",${capability}," in
      *,shared-sandbox-v1,*) ;;
      *) verdicts+=("runner $id did not attest shared-sandbox-v1 (reported '${capability:-no capability}'); check slots, runtime/helper pins, sandbox image labels and sandbox manager configuration") ;;
    esac
    case ",${capability}," in
      *,native-fork-at-anchor-v1,*) ;;
      *) verdicts+=("runner $id did not attest native-fork-at-anchor-v1 (reported '${capability:-no capability}'); historical Fork/Edit & rewind is not deployment-ready") ;;
    esac
  done
  if [[ "${#verdicts[@]}" -gt 0 ]]; then
    # The tail of stderr, not the head: the exception and its message are the
    # last lines of a Python traceback, and the first ones are stack frames.
    # Each replica's output goes to its own file, so a failure prints all of
    # them instead of whichever one happened to run first.
    for index in "${!attest_ids[@]}"; do
      id="${attest_ids[index]}"
      if [[ -s "${ATTEST_DIR}/${id}.stderr" ]]; then
        printf '[sandbox-host] %s said:\n' "$id" >&2
        tail -n 40 "${ATTEST_DIR}/${id}.stderr" >&2 || true
      fi
    done
    die "$(printf '%s\n' "${verdicts[@]}")"
  fi
  # A host that claims turns it cannot start takes the queue apart one claim
  # at a time, so prove the image can start one. A turn process with nothing on
  # its standard input must come up, import everything a turn needs, pass the
  # same read-only Docker/image/network preflight a real turn does, and then
  # decline cleanly — which is exactly the path a turn takes up to the moment
  # it would begin, and no further: nothing is claimed and no sandbox is
  # touched. A broken image fails here instead of after the readiness marker
  # is published.
  while ! turn_probe="$(docker exec "$first_id" python -m cloud_agent_host.runner \
    --turn --handover-fd 1 2>&1)"; do
    (( SECONDS < deadline )) \
      || die "runner $first_id cannot start a turn process: ${turn_probe:-no output}"
    log "waiting for the runner to be able to start a turn: ${turn_probe:-no output}"
    sleep 3
  done
  # The verdict is the child's own answer, the one line it writes to the
  # descriptor this probe pointed at its standard output. Everything else the
  # process says on the way — the logging every turn process configures, a
  # Kata guest measurement warning from the same preflight a real turn runs —
  # goes to stderr, and stderr is captured too because it is what explains a
  # failure. So the answer is read as a line, not as the whole of the output:
  # on staging the first rollout of this probe captured such a warning ahead
  # of the decline, called the image unable to run turns, and rolled back.
  turn_answer="$(printf '%s\n' "$turn_probe" | grep -E '^declined' | head -n 1 || true)"
  if [[ -n "$turn_answer" ]]; then
    log "turn processes start: ${turn_answer}"
  else
    die "runner $first_id started a turn process that answered '${turn_probe:-nothing}' instead of declining an empty turn; the image cannot run turns"
  fi

  # Capability attestation proves the runner is configured to dial the sandbox manager;
  # make the dial as well. This catches a sandbox manager crash-loop (including a
  # DockerRootDir mismatch) before the control plane begins assigning chats.
  local sandbox_manager_root expected_root control_status recent_runner_logs
  while ! sandbox_manager_root="$(docker exec "$first_id" python -c \
    'import json, os, urllib.request; url = os.environ["AGENT_SANDBOX_MANAGER_URL"].rstrip("/") + "/health"; request = urllib.request.Request(url, headers={"X-Agent-Workspace-Token": os.environ["AGENT_SANDBOX_MANAGER_TOKEN"]}); print(json.load(urllib.request.urlopen(request, timeout=10)).get("retained_docker_data_root") or "")')"; do
    (( SECONDS < deadline )) \
      || die "runner $first_id cannot authenticate to the sandbox manager health endpoint"
    log "sandbox manager is not ready yet; retrying"
    sleep 3
  done
  expected_root="$(env_or_dotenv AGENT_RETAINED_DOCKER_DATA_ROOT)"
  expected_root="$(normalise_host_path "${expected_root:-/var/lib/docker}")"
  sandbox_manager_root="$(normalise_host_path "$sandbox_manager_root")"
  [[ "$sandbox_manager_root" == "$expected_root" ]] \
    || die "sandbox manager reports retained DockerRootDir '${sandbox_manager_root:-unset}', expected '$expected_root'"

  # The inference-gateway probe above is a different tunnel. Prove the runner
  # can also reach the control plane that receives its capability heartbeat.
  # The control-plane deployment performs the authoritative second half: it
  # waits for a fresh database heartbeat and dials this sandbox manager in reverse.
  while ! control_status="$(docker exec "$first_id" python -c \
    'import os, urllib.request; url = os.environ["AGENT_CONTROL_PLANE_URL"].rstrip("/") + "/healthz"; print(urllib.request.urlopen(url, timeout=10).status)')"; do
    (( SECONDS < deadline )) \
      || die "runner $first_id cannot reach the control-plane health endpoint"
    log "control plane is not ready through its tunnel yet; waiting for the parallel deployment"
    sleep 3
  done
  [[ "$control_status" == "200" ]] \
    || die "control-plane health endpoint returned '${control_status:-no status}'"

  # A rejected persisted credential is definitive, but the runner deliberately
  # stays alive and stops claiming. Container health alone would call that host
  # ready forever, so surface the heartbeat thread's terminal verdict here.
  recent_runner_logs="$(compose logs --since 2m runner 2>&1 || true)"
  if grep -q "agent_host_credential_rejected" <<<"$recent_runner_logs"; then
    die "the control plane rejected this host's persisted credential; re-enrol the host before enabling retained chats"
  fi

  # Storage attestation last, after every check that can refuse this host.
  #
  # It is not part of the fatal set — a metric that cannot be established is no
  # reason to keep a working host out of the pool — but "not fatal" is a claim
  # about its own exit status, not about its effect. The probes above retry
  # against this same deadline, so seconds spent here are seconds they do not
  # get: run before them, a slow `docker exec` for a disk figure can leave the
  # control-plane dial one attempt where it had eight, and a deployment then
  # rolls back over a number nobody needed. Running last, the attestation can
  # only spend what the host did not need to prove itself, and running out
  # costs exactly the metric.
  verify_storage_probe "$ATTEST_DIR" "$runner_ids" "$deadline" "$fenced"
  cleanup_attest_dir

  log "host-local shared-live ready: the runner attests shared-sandbox-v1 and native-fork-at-anchor-v1, can start a turn process, reaches the control plane, and sandbox manager storage is ${sandbox_manager_root}"
}

# ── Isolation ─────────────────────────────────────────────────────────
#
# Ported at H4 from the gateway's own runner script, which was deleted with the
# rest of its agent tooling. The gateway had two runner scripts and only this
# one migrated; the check lived in the other. Without this port the split would
# have left **no runner script anywhere that verifies its own isolation
# boundary** — while the sandbox image, the compose file and the docs all still
# say Kata. `git log` in the source repository has the original.
#
# The default backend is a claim about what every job gets. These two checks
# cover the ways that claim is false while everything still looks fine.

KATA_RUNTIME="io.containerd.kata.v2"
KATA_SETUP_SCRIPT="${KATA_SETUP_SCRIPT:-deploy/host/setup_kata_runtime.sh}"
if [[ "$AGENT_DEPLOY_MODE" == "artifacts" && "$KATA_SETUP_SCRIPT" == "deploy/host/setup_kata_runtime.sh" ]]; then
  KATA_SETUP_SCRIPT="$SCRIPT_DIR/setup_kata_runtime.sh"
fi

warn() { printf '[sandbox-host] WARNING: %s\n' "$*" >&2; }

require_kata_host() {
  # Gate before anything starts. The runner's own preflight would fail per job
  # otherwise, which reads as "the queue is broken" rather than "this host was
  # never provisioned" — and that misreading is precisely how staging ended up
  # running every job on a shared kernel for months.
  local backend
  backend="$(env_or_dotenv AGENT_SANDBOX_BACKEND)"
  backend="${backend:-kata}"

  if [[ "$backend" != "kata" ]]; then
    # Not silently tolerated: this is the deployment saying out loud that it
    # has no kernel boundary, so nobody reads `AGENT_SANDBOX_BACKEND=container`
    # in an .env six months from now and assumes it was reviewed.
    warn "AGENT_SANDBOX_BACKEND=${backend}: sandboxes on this host share the host kernel."
    warn "Repository code runs against the host's own syscall surface. Acceptable for"
    warn "trusted repositories only. For untrusted or multi-tenant work, install Kata"
    warn "(sudo ${KATA_SETUP_SCRIPT}) and drop the override."
    return 0
  fi

  [[ -x "$KATA_SETUP_SCRIPT" ]] || die "missing ${KATA_SETUP_SCRIPT}; cannot verify this host's Kata runtime"
  if ! "$KATA_SETUP_SCRIPT" --check; then
    die "this host is not provisioned for Kata. Install it with:
    sudo ${KATA_SETUP_SCRIPT}
  Or accept a shared host kernel *explicitly* by setting AGENT_SANDBOX_BACKEND=container
  in .env. There is deliberately no automatic fallback: silently downgrading the
  isolation boundary is the one failure mode nobody would notice."
  fi
}

kata_smoke_test() {
  # The authoritative check, and the one nothing else can stand in for: start a
  # real container under the real runtime and ask which kernel it is running.
  # Under runc that is the host's own; under Kata it is the guest's. Every
  # cheaper signal (the flag is accepted, the shim exists) has already passed on
  # hosts where jobs were still sharing the host kernel.
  local backend host_kernel guest_kernel
  backend="$(env_or_dotenv AGENT_SANDBOX_BACKEND)"
  backend="${backend:-kata}"
  [[ "$backend" == "kata" ]] || return 0

  host_kernel="$(uname -r)"
  log "starting one container under ${KATA_RUNTIME} to confirm it gets its own kernel"
  if ! guest_kernel="$(docker run --rm --runtime "$KATA_RUNTIME" --entrypoint /bin/sh \
    "$SANDBOX_IMAGE" -c 'uname -r' 2>&1)"; then
    die "could not start a container under ${KATA_RUNTIME}: ${guest_kernel}
  The shim is installed but the runtime does not work. '${KATA_SETUP_SCRIPT} --check'
  and 'kata-runtime check' will say more."
  fi
  guest_kernel="$(printf '%s' "$guest_kernel" | tail -1 | tr -d '[:space:]')"

  if [[ "$guest_kernel" == "$host_kernel" ]]; then
    die "the sandbox reported the host's own kernel (${host_kernel}), so it is NOT
  VM-isolated even though the runtime was accepted. Refusing to start runners:
  a deployment that believes it has a kernel boundary and does not is worse than
  one that knows it does not."
  fi
  log "VM isolation confirmed: guest kernel ${guest_kernel}, host kernel ${host_kernel}"
}

cmd_preflight() {
  command -v docker >/dev/null || die "docker is not installed on this host"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (or not accessible)"
  validate_retained_config
  require_kata_host
  kata_smoke_test
}

initialize_kata_epoch() {
  local runner_image="$1" backend="$2" runtime_root state_dir image_id
  [[ "$backend" == "kata" ]] || return 0
  runtime_root="${KATA_EXTRACT_ROOT:-/}"
  state_dir="${runtime_root%/}/var/lib/freeinference/kata-runtime"
  # Existing hosts predate the epoch file. This check is verification-only;
  # an installer racing it publishes its own generation under the same lock,
  # which the initializer preserves rather than replacing with a stale verdict.
  "$KATA_SETUP_SCRIPT" --check || die "cannot initialize Kata runtime epoch before host verification"
  python3 "$SCRIPT_DIR/kata_runtime_epoch.py" verify-path --root "$runtime_root" \
    || die "Kata runtime epoch path is not trusted"
  image_id="$(docker image inspect --format '{{.Id}}' "$runner_image")"
  [[ "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]] || die "cannot resolve the trusted epoch initializer image"
  log "initializing the host-wide Kata runtime epoch if absent"
  # Docker creates only this dedicated directory when it is missing. The
  # deploy user need not own /var/lib; root inside this networkless helper
  # owns the marker. No Docker socket or runtime/config tree is mounted.
  docker run --rm -i --pull never --runtime runc --network none \
    --user 0:0 --cap-drop ALL --security-opt no-new-privileges --read-only \
    -v "$state_dir:/run/agent-kata-runtime" --entrypoint python \
    "$image_id" - initialize --mounted <"$SCRIPT_DIR/kata_runtime_epoch.py" \
    || die "Kata epoch initialization failed; repair the runtime before deploying"
}

check_candidate() {
  # Compose resolves both the source-built name and artifact-mode digest.
  # Use a separate container with no live stack mounts or credentials. This
  # runs the full Kata observer probe before the first disruptive operation,
  # including on a cold host where no Compose network or sandbox manager exists yet.
  local runner_image backend
  # --images runner includes dependency images too. Read this service only;
  # a source build without an explicit image uses Compose's project-service tag.
  runner_image="$(compose config --format json | python3 -c \
    'import json, sys; model = json.load(sys.stdin); print(model["services"]["runner"].get("image") or model["name"] + "-runner")')"
  [[ -n "$runner_image" && "$runner_image" != *$'\n'* ]] \
    || die "could not resolve exactly one candidate runner image"
  backend="$(env_or_dotenv AGENT_SANDBOX_BACKEND)"
  log "checking candidate kernel, guest observer and image attestation before draining"
  docker run --rm --network none \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -e "AGENT_SANDBOX_BACKEND=${backend:-kata}" \
    -e "AGENT_SANDBOX_IMAGE=$SANDBOX_IMAGE" \
    "$runner_image" python -m cloud_agent_host.candidate_check
  # The network settings, read by the parser the new runners will use, and
  # nothing more: no socket, no network, no rules installed. A pool that is
  # public, a resolver inside the pool, or a relay given as an address would
  # otherwise surface only as runners refusing to start, after the old ones
  # were drained.
  log "checking this deployment's sandbox network settings with the candidate's parser"
  docker run --rm --network none \
    -e "AGENT_SANDBOX_NAMESPACE=$COMPOSE_PROJECT" \
    -e "AGENT_RELAY_NETWORK=$RELAY_NETWORK" \
    -e "AGENT_EGRESS_CHAT_NETWORK_POOL=$(env_or_dotenv AGENT_EGRESS_CHAT_NETWORK_POOL)" \
    -e "AGENT_EGRESS_DNS_SERVER=$(env_or_dotenv AGENT_EGRESS_DNS_SERVER)" \
    -e "AGENT_EGRESS_PROTECTED_CIDRS=$(env_or_dotenv AGENT_EGRESS_PROTECTED_CIDRS)" \
    -e "AGENT_GATEWAY_URL=http://${GATEWAY_HOSTNAME}:${TUNNEL_PORT}" \
    -e "AGENT_SANDBOX_MCP_URL=http://${MCP_HOSTNAME}:${MCP_TUNNEL_PORT}" \
    "$runner_image" python -c 'from cloud_agent_shared.egress import network_from_env; network_from_env()' \
    || die "the sandbox network settings were refused (see the error above); fix AGENT_EGRESS_CHAT_NETWORK_POOL, AGENT_EGRESS_DNS_SERVER or AGENT_EGRESS_PROTECTED_CIDRS before any runner is drained"
  initialize_kata_epoch "$runner_image" "${backend:-kata}"
}

# This machine's name in the admin host switch. Resolved here because the
# script runs on the host: inside a container the hostname is a container id,
# so a runner cannot answer "which machine am I" for itself.
# One server may run several independently enrolled execution environments.
# Report the server separately; container hostnames and deployment aliases
# cannot identify the physical machine in the admin inventory.
#
# A function rather than two lines inside the rollout, because the rollout is
# two phases that a deployer may run as two processes: these are exported for
# the `compose up` in `activate`, and resolving them only in `prepare` would
# start the runners of a phased deployment with no name for their machine.
resolve_host_identity() {
  AGENT_PHYSICAL_HOST="$(env_or_dotenv AGENT_PHYSICAL_HOST)"
  if [[ -z "$AGENT_PHYSICAL_HOST" ]]; then
    AGENT_PHYSICAL_HOST="$(hostname -s 2>/dev/null || hostname)"
  fi
  export AGENT_PHYSICAL_HOST

  if [[ -z "$(env_or_dotenv AGENT_RUNNER_HOST)" ]]; then
    AGENT_RUNNER_HOST="$(hostname -s 2>/dev/null || hostname)"
    export AGENT_RUNNER_HOST
    log "this machine joins the host pool as '${AGENT_RUNNER_HOST}'" \
        "(set AGENT_RUNNER_HOST in .env to rename it)"
  fi
}

# Everything a rollout does while the generation it replaces is still serving.
#
# The boundary is the one the comment below `check_candidate` has always drawn:
# build or pull every replacement, prove the candidate kernel and guest
# observer, and only then invalidate the marker and drain. Naming the two halves
# is what lets a deployer put something *between* them — which is what the
# release design asks for, and what no caller could express while `up` was the
# only entry point (#270).
#
# This phase writes caches and creates temporary probe containers. It replaces
# nothing that is serving, and `up` runs it exactly as it always did.
cmd_prepare() {
  if [[ $# -gt 0 ]]; then
    die "'prepare' takes no arguments"
  fi
  command -v docker >/dev/null || die "docker is not installed on this host"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (or not accessible)"
  require_env
  validate_retained_config
  # The cheap host check stays before the build. The authoritative guest-kernel
  # probe needs the just-built image and therefore runs immediately after it;
  # this also makes the first deployment work when no old image exists.
  require_kata_host

  resolve_host_identity

  if [[ "$AGENT_DEPLOY_MODE" == "artifacts" ]]; then
    log "pulling sandbox image (${SANDBOX_IMAGE})"
    docker pull "$SANDBOX_IMAGE"
  else
    # Only extra, operator-approved menu entries are passed. The defaults fall
    # through to the Dockerfile's reviewed pins and are attested as one protocol
    # unit by both the runner and control plane.
    local build_args=() name
    for name in CLAUDE_CODE_EXTRA_VERSIONS CODEX_EXTRA_VERSIONS PI_EXTRA_VERSIONS \
                OPENCODE_EXTRA_VERSIONS KILO_EXTRA_VERSIONS; do
      [[ -n "${!name}" ]] || continue
      log "runtime menu: ${name}=${!name} (adds to the reviewed Dockerfile default)"
      build_args+=(--build-arg "${name}=${!name}")
    done

    log "building the sandbox image (${SANDBOX_IMAGE})"
    # `--provenance=false` is load-bearing, not tidiness. Buildx attaches a
    # provenance attestation by default and regenerates it on *every* build, so
    # rebuilding an unchanged image yields the same config layer and a different
    # manifest index. On a daemon with the containerd image store — this host,
    # Docker 29.2.1 — that index digest is the image's identity, which makes two
    # consequences follow from a build that changed nothing:
    #
    #   * Compose sees every image it rebuilt as new and replaces every container
    #     built from it. Measured before this flag: all eight containers of the
    #     sandbox stack replaced on every deploy, 10.3s of it the since-removed
    #     Squid proxy sitting out Docker's stop timeout.
    #   * `sandbox_image_digest` — which the runner attests and a retained group
    #     records immutably — moves, and the previous value stops resolving.
    #     Verified on staging: the index a deploy produced at 14:07 was
    #     `No such image` eight minutes later.
    #
    # It has to be this flag on this command line. The compose file's
    # `build.provenance` is accepted by `docker compose config` and ignored by
    # `docker compose build`; `SOURCE_DATE_EPOCH` and `BUILDKIT_PROVENANCE` were
    # both measured and change nothing. With the flag, two builds of the real
    # runner image, every layer CACHED, return the same id.
    #
    # The `+` guard is load-bearing under `set -u`: with no overrides at all,
    # a bare `"${build_args[@]}"` is an unbound reference on bash before 4.4,
    # and this script also runs on developer machines.
    docker build ${build_args[@]+"${build_args[@]}"} --provenance=false \
      -f deploy/sandbox/Dockerfile.agent-sandbox -t "$SANDBOX_IMAGE" .
  fi

  kata_smoke_test

  # Build every replacement while the old, proven generation is still serving.
  # Only after all builds and the Kata probe succeed do we invalidate its
  # marker and drain runners. That keeps a failed build from pausing the queue
  # and prevents Compose from replacing the sandbox manager underneath an active turn.
  if [[ "$AGENT_DEPLOY_MODE" == "artifacts" ]]; then
    log "pulling host release images"
    compose pull
  else
    # `--provenance=false` for the reason written out at the sandbox image
    # above: a regenerated attestation makes an unchanged rebuild a new image as
    # far as Compose is concerned, and it then replaces every container it built.
    log "building host services"
    AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" \
      compose build --provenance=false
  fi
  check_candidate
}

# Resolve every project-owned process before mutating any of them, then let
# each process finish its own bounded shutdown work. In particular the sandbox manager
# archives every retained group from its lifespan finalizer; a Compose grace
# period cannot safely represent that serial work.
drain_runners_then_sandbox_manager() {
  local runner_ids_text sandbox_manager_ids_text legacy_ids_text
  local -a runner_ids=()
  local -a sandbox_manager_ids=()
  local -a legacy_ids=()
  if ! runner_ids_text="$(compose ps -q runner)"; then
    die "could not resolve the existing runner containers; refusing to stop the stack"
  fi
  if ! sandbox_manager_ids_text="$(compose ps -q sandbox-manager)"; then
    die "could not resolve the existing sandbox-manager containers; refusing to stop the stack"
  fi
  # The old service is absent from the new Compose file. Resolve it by exact
  # project/service labels before starting another manager for the same groups.
  if ! legacy_ids_text="$(docker ps -aq \
    --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
    --filter 'label=com.docker.compose.service=workspace-broker')"; then
    die "could not resolve the previous sandbox manager service; refusing to stop the stack"
  fi
  if [[ -n "$runner_ids_text" ]]; then
    mapfile -t runner_ids <<< "$runner_ids_text"
  fi
  if [[ -n "$sandbox_manager_ids_text" ]]; then
    mapfile -t sandbox_manager_ids <<< "$sandbox_manager_ids_text"
  fi
  if [[ -n "$legacy_ids_text" ]]; then
    mapfile -t legacy_ids <<< "$legacy_ids_text"
    sandbox_manager_ids+=("${legacy_ids[@]}")
  fi
  if (( ${#runner_ids[@]} > 0 )); then
    # The runner treats SIGTERM as a drain: it stops claiming, waits for every
    # child turn, then exits. Ask the Engine to wait indefinitely so activation
    # can never turn that graceful drain into SIGKILL. Resolve the IDs through
    # Compose first so this cannot stop an unrelated container on the host.
    log "draining existing runners without a forced-kill deadline"
    docker stop --time=-1 "${runner_ids[@]}"
  fi
  if (( ${#sandbox_manager_ids[@]} > 0 )); then
    # Runners settle first because active turns still use the sandbox manager. Once they
    # have gone, SIGTERM runs the sandbox manager lifespan finalizer and its own archive
    # deadlines govern every retained group instead of Compose's fixed grace.
    log "draining the existing sandbox manager without a forced-kill deadline"
    docker stop --time=-1 "${sandbox_manager_ids[@]}"
  fi
  if (( ${#legacy_ids[@]} > 0 )); then
    docker rm "${legacy_ids[@]}"
  fi
}

# The half that replaces things: invalidate the marker, drain the runners and
# sandbox manager, start the target generation and prove it.
#
# Assumes `prepare` has run in this deployment root — for the same release, on
# this machine. It does not re-check that, because the check would be the
# marker file this phase is about to write; what keeps the two together is the
# deployer calling them in order, and `up` doing so itself.
cmd_activate() {
  if [[ $# -gt 0 ]]; then
    die "'activate' takes no arguments"
  fi
  # Cheap, deterministic, and the reason it is not left to `prepare`: a phased
  # deployment runs the two halves as two processes, and these are exported for
  # the `compose up` below.
  resolve_host_identity
  prepare_shared_live_marker
  # Beside the readiness marker and before `up`: every runner has a read-only
  # bind of this file, and the empty entry set it starts from is what makes a
  # container that comes up before the checks finish report `unverified`
  # instead of the previous deployment's reading.
  prepare_storage_verdict
  drain_runners_then_sandbox_manager

  log "starting the three purpose-scoped tunnels, sandbox manager, and the runner service"
  # `--remove-orphans` because a renamed service leaves its old container
  # running otherwise: compose only replaces containers for services the file
  # still declares, so `agent-runner` outliving `runner` would mean two sets of
  # runners claiming from one host, which is the two-sandbox-managers incident again.
  AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" \
    compose up -d --no-build --remove-orphans

  # The probes below wait for what they need instead of being preceded by a
  # fixed pause: `compose up -d` has already created and started every
  # container, and a tunnel whose upstream is not answering yet fails its
  # first attempt and is retried three seconds later. The five seconds this
  # used to sleep were paid in full by every deployment — 5.1s measured from
  # `up -d` returning to the retained-storage line, with the first probe
  # succeeding immediately after it.
  #
  # In a subshell, because `die` calls `exit` and from a function that ends the
  # whole script — the logs below are exactly what explains a failed probe, so
  # they must still print. But the verdict is kept: swallowing it printed the
  # tunnel diagnosis, then "done", and an operator reads the last line.
  local readiness_wait probe_ok=0
  readiness_wait="$(env_or_dotenv SHARED_READY_TIMEOUT)"
  readiness_wait="${readiness_wait:-900}"
  [[ "$readiness_wait" =~ ^[1-9][0-9]*$ ]] \
    || die "SHARED_READY_TIMEOUT must be a positive integer (got '$readiness_wait')"
  ( cmd_check "$readiness_wait" ) || probe_ok=$?
  compose logs --tail 20 runner
  if [[ "$probe_ok" -ne 0 ]]; then
    die "the containers started but the deployment readiness check failed (see
     the probe output above). Fix the reported tunnel, sandbox manager, retained-storage
     or shared-live attestation error, then re-run 'check'."
  fi
  if [[ "${DEFER_SHARED_READY_MARKER:-0}" != "1" ]]; then
    printf '%s\n' "${AGENT_DEPLOYMENT_SHA}" > .deployed-sha \
      || die "could not record shared-live target ${AGENT_DEPLOYMENT_SHA} in .deployed-sha"
    complete_shared_live_marker
  fi
  log "done. 'deploy/host/sandbox_host.sh status' shows the live tail."
}

# Both halves, in order. What every caller before the split invoked, and what
# `deploy/host/deploy.sh` still invokes when it is given no phase.
cmd_up() {
  if [[ $# -gt 0 ]]; then
    die "'up' no longer takes a replica count: this host takes its concurrency limit from the admin Servers page, and AGENT_HOST_SLOTS in .env is only the default it starts with"
  fi
  cmd_prepare
  cmd_activate
}

cmd_check() {
  local wait_seconds="${1:-0}"
  [[ "$wait_seconds" =~ ^[0-9]+$ ]] \
    || die "host readiness wait must be a non-negative integer (got '$wait_seconds')"
  local deadline=$((SECONDS + wait_seconds))
  # A positive wait is a rollout's fence; `check` on its own passes zero and
  # has none. The storage attestation budgets against the difference, so the
  # two callers must not be conflated.
  local fenced=0
  if (( wait_seconds > 0 )); then fenced=1; fi
  validate_retained_config
  # Ask from the relay network, where group lifecycle finds the two relays it
  # attaches to every group bridge: reaching the gateway from this host's shell
  # proves nothing about the tunnel a sandbox will be handed. The group bridge
  # itself, with this deployment's host rules around it, is proved per group by
  # the guest check that runs before any repository code.
  local network="$RELAY_NETWORK"
  local url="http://${GATEWAY_HOSTNAME}:${TUNNEL_PORT}/health"
  log "probing ${url} from the ${network} network"
  probe_sandbox_relay "$network" "$url" "inference gateway" "$deadline" \
    "Check 'docker logs' for the gateway-tunnel container: an SSH connection that cannot authenticate or bind its forward restarts." "gateway-tunnel"

  # MCP is a second deliberately narrow destination on the same relay
  # network. A healthy model tunnel does not prove this separate SSH key,
  # listener, alias, or forward. Probe it before publishing the exact-SHA
  # readiness marker.
  url="http://${MCP_HOSTNAME}:${MCP_TUNNEL_PORT}/healthz"
  log "probing ${url} from the ${network} network"
  probe_sandbox_relay "$network" "$url" "MCP-only relay" "$deadline" \
    "Check the mcp-tunnel SSH key, forward, and the control plane's MCP-only listener on port 8001." "mcp-tunnel"

  check_shared_live_stack "$deadline" "$fenced"
}

cmd_status() {
  compose ps
  printf '\n'
  compose logs --tail 20 \
    gateway-tunnel mcp-tunnel control-plane-tunnel \
    sandbox-manager runner
}

cmd_down() {
  if [[ $# -gt 0 ]]; then
    die "'down' takes no arguments"
  fi
  drain_runners_then_sandbox_manager
  compose down
  log "stopped after runners settled and the sandbox manager finished cleanup."
}

cmd_mark_ready() {
  complete_shared_live_marker "${1:-}"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  prepare) shift; cmd_prepare "$@" ;;
  activate) shift; cmd_activate "$@" ;;
  preflight) cmd_preflight ;;
  check) cmd_check 0 ;;
  status) cmd_status ;;
  down) shift; cmd_down "$@" ;;
  mark-ready) shift; cmd_mark_ready "$@" ;;
  *) die "usage: $0 {up|prepare|activate|preflight|check|status|down|mark-ready SHA}" ;;
esac
