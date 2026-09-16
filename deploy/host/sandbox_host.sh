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
# Setup dependency traffic has a separate route-less network whose only uplink
# is the allowlist proxy. Agent turns remain on the closed relay network.
#
# Usage, from the repository root on the runner host:
#
#   deploy/host/sandbox_host.sh up [N]   # build, start the tunnel + N runners
#   deploy/host/sandbox_host.sh status   # what is running, and the log tails
#   deploy/host/sandbox_host.sh check    # prove the tunnel reaches the gateway
#   deploy/host/sandbox_host.sh down     # stop everything this file started
#
# Required in the environment or in .env on this host:
#
#   COMPOSE_PROJECT                this stack's compose project, unique on this machine
#   AGENT_SANDBOX_IMAGE            image the sandboxes run
#   AGENT_TUNNEL_*                 inference tunnel destination/key/known_hosts
#   AGENT_MCP_TUNNEL_*             MCP-only tunnel destination/key/known_hosts
#   AGENT_CONTROL_PLANE_TUNNEL_*   control API tunnel destination/key/known_hosts
#   AGENT_BROKER_REVERSE_*         the control plane's route back to this broker
#   AGENT_WORKSPACE_BROKER_TOKEN   broker authentication secret
#
# Restrict the tunnel key on the gateway host — it needs one forwarded port and
# no shell:
#
#   command="",restrict,port-forwarding,permitopen="127.0.0.1:8080" ssh-ed25519 AAAA... agent-tunnel

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

env_or_dotenv() {
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

case "${1:-}" in
  up|check|status|down|mark-ready) load_compose_environment ;;
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
    docker compose \
      -p "$COMPOSE_PROJECT" \
      -f "$COMPOSE_FILE" \
      --env-file .env "$@"
  else
    # Compose already receives exported configuration from this process. Do not
    # pass a path that does not exist: env-only hosts are an explicitly supported
    # direct-operator path, even though the CD wrapper deliberately requires a
    # deploy-root .env so credentials survive non-interactive service restarts.
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
EGRESS_NETWORK="$(env_or_dotenv AGENT_EGRESS_NETWORK_PLATFORM_ONLY)"
EGRESS_NETWORK="${EGRESS_NETWORK:-agent-egress}"

# These two networks are new to the standalone stack, so unlike the historical
# platform-only network they can safely derive collision-free names from the
# Compose project. An explicit value still wins. The runner creates chat
# containers through the Docker API, outside Compose, so it must receive the
# exact daemon-side name Compose creates.
TRUSTED_EGRESS_NETWORK="$(env_or_dotenv AGENT_EGRESS_NETWORK_TRUSTED)"
TRUSTED_EGRESS_NETWORK="${TRUSTED_EGRESS_NETWORK:-${COMPOSE_PROJECT:+${COMPOSE_PROJECT}-}agent-egress-trusted}"
AGENT_EGRESS_NETWORK_TRUSTED="$TRUSTED_EGRESS_NETWORK"
export AGENT_EGRESS_NETWORK_TRUSTED
UPLINK_EGRESS_NETWORK="$(env_or_dotenv AGENT_EGRESS_NETWORK_UPLINK)"
UPLINK_EGRESS_NETWORK="${UPLINK_EGRESS_NETWORK:-${COMPOSE_PROJECT:+${COMPOSE_PROJECT}-}agent-egress-uplink}"
AGENT_EGRESS_NETWORK_UPLINK="$UPLINK_EGRESS_NETWORK"
export AGENT_EGRESS_NETWORK_UPLINK

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
              AGENT_WORKSPACE_BROKER_TOKEN \
              AGENT_TUNNEL_SSH_DESTINATION AGENT_TUNNEL_SSH_KEY \
              AGENT_TUNNEL_SSH_KNOWN_HOSTS \
              AGENT_MCP_TUNNEL_SSH_DESTINATION AGENT_MCP_TUNNEL_SSH_KEY \
              AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS \
              AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION \
              AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY \
              AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS \
              AGENT_BROKER_REVERSE_BIND AGENT_BROKER_REVERSE_PORT \
              AGENT_WORKSPACE_BROKER_ADVERTISE_URL; do
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
    for name in AGENT_HOST_STATE_ROOT AGENT_EGRESS_NETWORK_PLATFORM_ONLY; do
      path="$(env_or_dotenv "$name")"
      case "$name" in
        AGENT_HOST_STATE_ROOT)
          unsafe="$(python3 -c 'import os; print(os.path.realpath("/var/lib/hybridinference/agent-host"))')"
          if [[ -n "$path" ]]; then
            path="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$path")"
          fi
          ;;
        AGENT_EGRESS_NETWORK_PLATFORM_ONLY) unsafe="agent-egress" ;;
      esac
      if [[ -z "$path" || "$path" == "$unsafe" ]]; then
        shared+=("$name")
      fi
    done
    if [[ ${#shared[@]} -gt 0 ]]; then
      die "this host runs another environment (SIBLING_COMPOSE_PROJECTS is set), so ${shared[*]} must be unique to this stack; unset values and legacy shared defaults mix enrollment credentials or tunnel aliases across environments."
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

  # This compose file is the standalone/remote-host topology: its broker has no
  # published host port and shares no Docker network with the control-plane
  # stack. The reverse forward is therefore not an optional optimisation. A
  # missing half would let the host attest locally, then make the control deploy
  # wait the full readiness timeout on an address it can never reach.
  local reverse_bind reverse_port advertise_url
  reverse_bind="$(env_or_dotenv AGENT_BROKER_REVERSE_BIND)"
  reverse_port="$(env_or_dotenv AGENT_BROKER_REVERSE_PORT)"
  advertise_url="$(env_or_dotenv AGENT_WORKSPACE_BROKER_ADVERTISE_URL)"
  case "$reverse_bind" in
    0.0.0.0|127.0.0.1|localhost|::|::1)
      die "AGENT_BROKER_REVERSE_BIND=$reverse_bind is not the control-plane Docker bridge gateway: loopback is invisible to its containers and a wildcard exposes the broker to the host network"
      ;;
  esac
  [[ "$reverse_port" =~ ^[0-9]+$ \
     && "$reverse_port" -ge 1 \
     && "$reverse_port" -le 65535 ]] \
    || die "AGENT_BROKER_REVERSE_PORT must be an integer from 1 through 65535 (got '$reverse_port')"
  [[ "$advertise_url" =~ ^https?://[^/]+(:[0-9]+)?(/.*)?$ ]] \
    || die "AGENT_WORKSPACE_BROKER_ADVERTISE_URL must be an http(s) URL reachable by the control plane (got '$advertise_url')"
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

  local slots runner_replicas resident_capacity chat_idle_timeout
  slots="$(env_or_dotenv AGENT_HOST_SLOTS)"
  slots="${slots:-3}"
  [[ "$slots" =~ ^[0-9]+$ && "$slots" -ge 2 ]] \
    || die "AGENT_HOST_SLOTS must be an integer >= 2 (got '${slots:-unset}')"
  runner_replicas="$(env_or_dotenv AGENT_RUNNER_REPLICAS)"
  runner_replicas="${runner_replicas:-3}"
  [[ "$runner_replicas" =~ ^[0-9]+$ && "$runner_replicas" -ge 2 ]] \
    || die "AGENT_RUNNER_REPLICAS must be an integer >= 2 (got '${runner_replicas:-unset}')"
  [[ "$slots" -le "$runner_replicas" ]] \
    || die "AGENT_HOST_SLOTS=$slots overstates AGENT_RUNNER_REPLICAS=$runner_replicas; one runner executes one turn, so shared-live slots cannot exceed executors"
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

  # The Trusted network is closed and the proxy alone straddles it and the
  # routable uplink. Reusing a name here collapses that boundary and can put a
  # chat directly on an internet-routable bridge while the config still says
  # `trusted`.
  if [[ "$EGRESS_NETWORK" == "$TRUSTED_EGRESS_NETWORK" \
     || "$EGRESS_NETWORK" == "$UPLINK_EGRESS_NETWORK" \
     || "$TRUSTED_EGRESS_NETWORK" == "$UPLINK_EGRESS_NETWORK" ]]; then
    die "platform-only ('$EGRESS_NETWORK'), trusted ('$TRUSTED_EGRESS_NETWORK'), and proxy-uplink ('$UPLINK_EGRESS_NETWORK') networks must be three distinct names"
  fi
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
  # The platform-only name predates project-derived defaults and is immutable
  # for retained containers already attached to it. Inventing a new name on
  # upgrade would leave stopped chats on the old network while moving their
  # relays away, so preserve an explicitly provisioned per-stack name.
  if [[ -n "$(env_or_dotenv SIBLING_COMPOSE_PROJECTS)" \
     && -z "$(env_or_dotenv AGENT_EGRESS_NETWORK_PLATFORM_ONLY)" ]]; then
    die "this host runs another environment, so AGENT_EGRESS_NETWORK_PLATFORM_ONLY must name this stack's own existing closed network. Reusing 'agent-egress' lets staging and production resolve each other's relays; changing it blindly strands retained containers on the old network."
  fi

  # Chat files live in Docker's writable layer. Verify the daemon whose socket
  # the broker receives is the daemon backed by the configured storage root;
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
# **Nothing here is fatal.** A page that cannot say how full a disk is must not
# be able to stop the work that fills it, so every failure below is a warning
# and an `unverified` entry. The one exception is the verdict file itself,
# which is a bind-mount source: without it the runner containers cannot start
# at all, and failing there names the problem instead of letting Compose's own
# error do it.

# The diagnostic band's floor. Occupancy moves between the two reads — builds
# and cleanup interleave, and nothing pauses the world for monitoring — so
# `used` and `available` are compared within a tolerance and only as a
# diagnosis. `size` does not move under writes, and it is the actual test.
STORAGE_DIAGNOSTIC_FLOOR_BYTES=$((1024 * 1024 * 1024))

storage_verdict_file() {
  # Beside .shared-live-ready and .deployed-sha, for the same reason: the
  # deployment user owns this checkout, and each runner mounts the file
  # read-only rather than needing access to credentials or host state.
  printf '%s/.storage-probe-verdict' "${PWD%/}"
}

storage_probe_name() {
  # The deployment declares the topology; the collector never guesses it. An
  # absent value takes the same default the compose file interpolates.
  local probe
  probe="$(env_or_dotenv AGENT_HOST_STORAGE_PROBE)"
  printf '%s' "${probe:-overlay_root}"
}

prepare_storage_verdict() {
  # An empty entry set before `compose up`, overwritten in place after the
  # checks: every runner holds a read-only file bind of this path, so replacing
  # the file by rename would leave running replicas reading the old inode.
  # Fail-closed on purpose — a runner that starts while the checks are still
  # running reads "unverified", never the previous deployment's verdict.
  local verdict_file written_at
  verdict_file="$(storage_verdict_file)"
  written_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '{"schema_version":1,"written_at":"%s","deployment_sha":"%s","entries":{}}\n' \
    "$written_at" "${AGENT_DEPLOYMENT_SHA:-}" >"$verdict_file" \
    || die "could not write the storage-probe verdict to $verdict_file"
  chmod 0644 "$verdict_file" \
    || die "could not make the storage-probe verdict readable by runner replicas"
}

storage_quota_configured() {
  # Best effort, and known to be: `df` cannot distinguish a project quota on
  # the mount root itself from the disk, so the configuration is checked as
  # well. `overlay2.size` is the only quota this deployment can impose.
  #
  # The bracket in the pattern is load-bearing: `ps` snapshots the grep process
  # too, and a pattern spelled with a literal dot would match its own command
  # line and report a quota on every host.
  local daemon_json="${DOCKER_DAEMON_JSON:-/etc/docker/daemon.json}"
  if [[ -r "$daemon_json" ]] && grep -q 'overlay2[.]size' "$daemon_json" 2>/dev/null; then
    return 0
  fi
  if ps -eo args= 2>/dev/null | grep -q -- 'overlay2[.]size'; then
    return 0
  fi
  return 1
}

storage_df_size() {
  # Bytes of the filesystem containing a path, as GNU df reports it. `-B1`
  # with `--output=size` prints one exact integer: no human rounding, and no
  # dependence on the terminal's idea of a block. Prints nothing when df
  # cannot answer, so the caller can tell "no answer" from a real zero.
  local value
  value="$(df -B1 --output=size "$1" 2>/dev/null | tail -n1 | tr -d '[:space:]' || true)"
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

storage_usage_agrees() {
  # Compare the container's occupancy figures with the host's, up to three
  # times, and print a note describing the last discrepancy. Returns 0 when the
  # two agree within max(1 GiB, 1% of the size).
  #
  # Used and available are diagnostics rather than a topology test: they move
  # between the two reads because builds and cleanup interleave, so requiring
  # equality would fail on any busy host. A monotonic assumption is not
  # available either — nothing here pauses the world for monitoring.
  local id="$1" mount_root="$2" size="$3" attempt tolerance
  local container_used container_avail host_used host_avail delta_used delta_avail
  local note="the reading could not be taken" container_line host_line
  tolerance=$(( size / 100 ))
  if (( tolerance < STORAGE_DIAGNOSTIC_FLOOR_BYTES )); then
    tolerance="$STORAGE_DIAGNOSTIC_FLOOR_BYTES"
  fi
  for attempt in 1 2 3; do
    container_line="$(
      docker exec "$id" python -m cloud_agent_host.host_capacity describe 2>/dev/null \
        | python3 -c 'import json, sys
try:
    reading = json.load(sys.stdin)
except ValueError:
    reading = {}
print(
    int(reading.get("total_bytes") or 0) - int(reading.get("free_bytes") or 0),
    int(reading.get("available_bytes") or 0),
)' || true
    )"
    host_line="$(df -B1 --output=used,avail "$mount_root" 2>/dev/null | tail -n1 || true)"
    read -r container_used container_avail <<<"$container_line" || true
    read -r host_used host_avail <<<"$host_line" || true
    container_used="${container_used:-0}"
    container_avail="${container_avail:-0}"
    host_used="${host_used:-0}"
    host_avail="${host_avail:-0}"
    if [[ ! "$container_used" =~ ^[0-9]+$ || ! "$container_avail" =~ ^[0-9]+$ \
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
      sleep 2
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
  # Takes the attestation directory and the runner ids from
  # check_shared_live_stack, which has already proven the replicas are alive —
  # a verdict about a container that is not running would be about nothing.
  local attest_dir="$1" runner_ids="$2"
  local entries_file verdict_file id index storage_rc ok reason upperdir fstype
  local total upper_size root_size host_fstype note verdict
  local -a ids=() pids=()

  if [[ "$(storage_probe_name)" == "off" ]]; then
    log "storage measurement is switched off; runners report it as disabled"
    return 0
  fi
  if [[ -z "$runner_ids" ]]; then
    warn "no runner replica is available to attest storage against"
    return 0
  fi

  # Asked in parallel and through the same command the runner samples with: a
  # shell re-implementation could agree with itself while both differ from
  # what the heartbeat will carry.
  for id in $runner_ids; do
    (
      storage_rc=0
      docker exec "$id" python -m cloud_agent_host.host_capacity describe \
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
    if [[ "$(cat "${attest_dir}/${id}.storage.status" 2>/dev/null || true)" != "0" ]]; then
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

    # The mount root, not the upper directory: a project quota on the upper
    # directory's project returns the quota from both, so the two would agree
    # while neither describes the disk.
    root_size=""
    mount_root="$(findmnt -n -o TARGET -T "$upperdir" 2>/dev/null | head -n1 || true)"
    # The filesystem type recorded in the verdict is the *host's*, not the
    # container's: inside the container `/` is an overlay, and the entry is
    # meant to name the filesystem the writable layers actually live on.
    host_fstype="$(findmnt -n -o FSTYPE -T "$upperdir" 2>/dev/null | head -n1 || true)"
    if [[ -z "$host_fstype" ]]; then
      host_fstype="$fstype"
    fi
    if [[ -z "$mount_root" || "$mount_root" != /* ]]; then
      verdict="unverified"
      append_storage_entry "$upperdir" "$verdict" "" "$host_fstype" "$total" \
        "the host could not locate the filesystem containing ${upperdir}" "$entries_file"
      warn "runner $id: findmnt could not locate the filesystem containing ${upperdir}"
      continue
    fi
    root_size="$(storage_df_size "$mount_root")"
    if [[ -z "$root_size" ]]; then
      append_storage_entry "$upperdir" "unverified" "$mount_root" "$host_fstype" "$total" \
        "df could not size ${mount_root} on the host" "$entries_file"
      warn "runner $id: df could not size ${mount_root}"
      continue
    fi
    if storage_quota_configured; then
      append_storage_entry "$upperdir" "unsupported" "$mount_root" "$host_fstype" "$root_size" \
        "the Docker daemon is configured with overlay2.size, so this reading is a project quota" \
        "$entries_file"
      warn "runner $id: daemon configuration sets overlay2.size; storage capacity would be a quota, not the disk"
      continue
    fi
    upper_size="$(storage_df_size "$upperdir")"
    if [[ "$upper_size" != "$root_size" ]]; then
      append_storage_entry "$upperdir" "unsupported" "$mount_root" "$host_fstype" "$root_size" \
        "df reports ${upper_size:-no} B for ${upperdir} but ${root_size} B for its mount root, which is a project quota" \
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
    if note="$(storage_usage_agrees "$id" "$mount_root" "$root_size")"; then
      append_storage_entry "$upperdir" "verified" "$mount_root" "$host_fstype" "$root_size" "" "$entries_file"
      log "runner $id: storage verified against ${mount_root} (${fstype}, ${root_size} B)"
    else
      append_storage_entry "$upperdir" "unverified" "$mount_root" "$fstype" "$root_size" \
        "the container's occupancy never matched the host's: ${note}" "$entries_file"
      warn "runner $id: occupancy readings did not agree; ${note}"
    fi
  done

  verdict_file="$(storage_verdict_file)"
  python3 - "$entries_file" "$verdict_file" "${AGENT_DEPLOYMENT_SHA:-}" <<'PY'
import datetime
import json
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
# Overwritten in place, never renamed: each runner holds a read-only file bind,
# so a new inode would be a file nobody is reading.
with open(verdict_file, "w", encoding="utf-8") as handle:
    json.dump(document, handle, sort_keys=True)
    handle.write("\n")
PY
  chmod 0644 "$verdict_file" || warn "could not relax the storage verdict's permissions"
  log "storage verdict written to ${verdict_file} ($(wc -l <"$entries_file" | tr -d '[:space:]') entries)"
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

wait_for_broker_health() {
  # The runners' preflight asks the broker for its retained sandboxes, and it
  # asks exactly once. `compose up -d` returns as soon as the containers are
  # created, so on a rollout that replaced the broker the replicas were judging
  # one that had been alive for a second: long enough to accept a connection,
  # not long enough to have finished reconciling the sandboxes it serves. Every
  # replica then reported "chat Session Supervisor is unavailable" and the
  # rollout failed over a stack that was healthy a few seconds later.
  #
  # Waiting here is what taking the fixed five-second pause out of this path was
  # supposed to leave behind: a wait on the fact the next step depends on rather
  # than on a timer. A broker that is already serving costs one exec.
  local deadline="${1:-$SECONDS}" broker_id
  broker_id="$(compose ps -q workspace-broker)" || broker_id=""
  [[ -n "$broker_id" ]] \
    || die "no workspace-broker replica is running; shared-live cannot be attested"
  while :; do
    # Any HTTP answer settles it, including the 401 an unauthenticated probe
    # gets: this waits for the server to be serving, not for it to authorize.
    # A refused connection is the state worth waiting out.
    if docker exec "$broker_id" python3 -c 'import urllib.error, urllib.request
try:
    urllib.request.urlopen("http://127.0.0.1:8092/health", timeout=2)
except urllib.error.HTTPError:
    pass' >/dev/null 2>&1; then
      return 0
    fi
    if (( SECONDS >= deadline )); then
      die "the workspace broker did not answer /health before the readiness deadline.
     'docker logs ${broker_id}' shows why it is not serving; a reconcile that
     raises at startup exits the process and every runner then reports the chat
     session supervisor as unavailable."
    fi
    sleep 1
  done
}

check_shared_live_stack() {
  local deadline="${1:-$SECONDS}"
  # Ask every running replica to complete the same executable preflight as its
  # claim loop, then attest the exact image labels, helper pins, runtime
  # drivers, broker settings and slot count used by the claim path.  Calling
  # preflight here is intentional: a freshly started runner may still be
  # blocked inside its own proxy/kernel/network probe while `docker exec`
  # already works.  Static capability inspection during that window would
  # publish the ready marker immediately before the runner crash-looped.
  local runner_ids running_replicas expected_replicas id capability first_id=""
  runner_ids="$(compose ps -q runner)" \
    || die "could not list runner replicas for shared-live verification"
  [[ -n "$runner_ids" ]] || die "no running runner replica is available to attest shared-sandbox-v1"
  running_replicas="$(wc -w <<<"$runner_ids" | tr -d '[:space:]')"
  expected_replicas="$(env_or_dotenv AGENT_RUNNER_REPLICAS)"
  expected_replicas="${expected_replicas:-3}"
  [[ "$running_replicas" -ge "$expected_replicas" ]] \
    || die "only $running_replicas of $expected_replicas configured runner replicas are running; shared-live concurrency is not ready"

  # Only once the replicas that will be asked are known to be there: this is
  # about what they are about to ask the broker, so it must not pre-empt a more
  # specific verdict about the replicas themselves.
  wait_for_broker_health "$deadline"

  # Replicas attest at the same time. The preflight is dominated by the real
  # request it makes through the tunnels, not by anything the replicas share,
  # and no replica reads another's answer — so the order the serial loop
  # imposed bought nothing and cost the sum of three waits: measured on
  # staging, 16.6s of a 46s host deployment. Verdicts are still reported per
  # runner, in listing order, so a failure names the container it came from
  # and the first replica is still the one asked about the broker below.
  #
  # Each replica's exit status goes to a file because `wait` is reaped in a
  # second pass, after every replica has been started — and it is captured with
  # `|| rc=$?` rather than read from `$?` after the command: under `set -e` a
  # failing `docker exec` ends this subshell where it stands, so a status
  # written after it would never be written at all, on the only path that reads
  # it. The code is carried into the verdict because 125/126/127 mean the
  # container or the exec itself is gone, which is a different problem from a
  # preflight that raised.
  ATTEST_DIR="$(mktemp -d)" \
    || die "could not create a directory for per-replica attestation output"
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
      *) verdicts+=("runner $id did not attest shared-sandbox-v1 (reported '${capability:-no capability}'); check slots, runtime/helper pins, sandbox image labels and broker configuration") ;;
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
  # Storage attestation, after the capability verdicts and before the directory
  # it uses is cleaned up. Deliberately not part of the fatal set above: see the
  # section comment — a metric that cannot be established is not a reason to
  # keep a working host out of the pool.
  verify_storage_probe "$ATTEST_DIR" "$runner_ids"
  cleanup_attest_dir

  # Capability attestation proves the runner is configured to dial the broker;
  # make the dial as well. This catches a broker crash-loop (including a
  # DockerRootDir mismatch) before the control plane begins assigning chats.
  local broker_root expected_root control_status recent_runner_logs
  while ! broker_root="$(docker exec "$first_id" python -c \
    'import json, os, urllib.request; url = os.environ["AGENT_WORKSPACE_BROKER_URL"].rstrip("/") + "/health"; request = urllib.request.Request(url, headers={"X-Agent-Workspace-Token": os.environ["AGENT_WORKSPACE_BROKER_TOKEN"]}); print(json.load(urllib.request.urlopen(request, timeout=10)).get("retained_docker_data_root") or "")')"; do
    (( SECONDS < deadline )) \
      || die "runner $first_id cannot authenticate to the workspace broker health endpoint"
    log "workspace broker is not ready yet; retrying"
    sleep 3
  done
  expected_root="$(env_or_dotenv AGENT_RETAINED_DOCKER_DATA_ROOT)"
  expected_root="$(normalise_host_path "${expected_root:-/var/lib/docker}")"
  broker_root="$(normalise_host_path "$broker_root")"
  [[ "$broker_root" == "$expected_root" ]] \
    || die "workspace broker reports retained DockerRootDir '${broker_root:-unset}', expected '$expected_root'"

  # The inference-gateway probe above is a different tunnel. Prove the runner
  # can also reach the control plane that receives its capability heartbeat.
  # The control-plane deployment performs the authoritative second half: it
  # waits for a fresh database heartbeat and dials this broker in reverse.
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

  log "host-local shared-live ready: every runner attests shared-sandbox-v1 and native-fork-at-anchor-v1, control plane is reachable, and broker storage is ${broker_root}"
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

check_candidate() {
  # Compose resolves both the source-built name and artifact-mode digest.
  # Use a separate container with no live stack mounts or credentials. This
  # runs the full Kata observer probe before the first disruptive operation,
  # including on a cold host where no Compose network or broker exists yet.
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
}

cmd_up() {
  local replicas="${1:-$(env_or_dotenv AGENT_RUNNER_REPLICAS)}"
  replicas="${replicas:-3}"
  [[ "$replicas" =~ ^[0-9]+$ ]] || die "replicas must be a number, got '$replicas'"
  AGENT_RUNNER_REPLICAS="$replicas"
  export AGENT_RUNNER_REPLICAS
  command -v docker >/dev/null || die "docker is not installed on this host"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (or not accessible)"
  require_env
  validate_retained_config
  # The cheap host check stays before the build. The authoritative guest-kernel
  # probe needs the just-built image and therefore runs immediately after it;
  # this also makes the first deployment work when no old image exists.
  require_kata_host

  # This machine's name in the admin host switch. Resolved here because the
  # script runs on the host: inside a container the hostname is a container id,
  # so a runner cannot answer "which machine am I" for itself.
  # One server may run several independently enrolled execution environments.
  # Report the server separately; container hostnames and deployment aliases
  # cannot identify the physical machine in the admin inventory.
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
    #     sandbox stack replaced on every deploy, 10.3s of it the egress proxy
    #     sitting out Docker's stop timeout.
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
  # and prevents Compose from replacing the broker underneath an active turn.
  if [[ "$AGENT_DEPLOY_MODE" == "artifacts" ]]; then
    log "pulling host release images"
    compose pull
  else
    # `--provenance=false` for the reason written out at the sandbox image
    # above: a regenerated attestation makes an unchanged rebuild a new image as
    # far as Compose is concerned, and it then replaces every container it built.
    log "building host services"
    AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" AGENT_RUNNER_REPLICAS="$replicas" \
      compose build --provenance=false
  fi
  check_candidate
  prepare_shared_live_marker
  # Beside the readiness marker and before `up`: every runner has a read-only
  # bind of this file, and the empty entry set it starts from is what makes a
  # container that comes up before the checks finish report `unverified`
  # instead of the previous deployment's reading.
  prepare_storage_verdict
  if [[ -n "$(compose ps -q runner 2>/dev/null || true)" ]]; then
    log "draining existing runners before replacing their workspace broker"
    compose stop runner
  fi

  log "starting the three purpose-scoped tunnels, egress proxy, broker, and ${replicas} runner(s)"
  # `--remove-orphans` because a renamed service leaves its old container
  # running otherwise: compose only replaces containers for services the file
  # still declares, so `agent-runner` outliving `runner` would mean two sets of
  # runners claiming from one host, which is the two-brokers incident again.
  AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" AGENT_RUNNER_REPLICAS="$replicas" \
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
     the probe output above). Fix the reported tunnel, broker, retained-storage
     or shared-live attestation error, then re-run 'check'."
  fi
  if [[ "${DEFER_SHARED_READY_MARKER:-0}" != "1" ]]; then
    printf '%s\n' "${AGENT_DEPLOYMENT_SHA}" > .deployed-sha \
      || die "could not record shared-live target ${AGENT_DEPLOYMENT_SHA} in .deployed-sha"
    complete_shared_live_marker
  fi
  log "done. 'deploy/host/sandbox_host.sh status' shows the live tail."
}

cmd_check() {
  local wait_seconds="${1:-0}"
  [[ "$wait_seconds" =~ ^[0-9]+$ ]] \
    || die "host readiness wait must be a non-negative integer (got '$wait_seconds')"
  local deadline=$((SECONDS + wait_seconds))
  validate_retained_config
  # Ask from inside the closed network, which is the only vantage point whose
  # answer means anything: reaching the gateway from this host's shell proves
  # nothing about what a sandbox can reach.
  local network="$EGRESS_NETWORK"
  local url="http://${GATEWAY_HOSTNAME}:${TUNNEL_PORT}/health"
  log "probing ${url} from the ${network} network"
  probe_sandbox_relay "$network" "$url" "inference gateway" "$deadline" \
    "Check 'docker logs' for the gateway-tunnel container: an SSH connection that cannot authenticate or bind its forward restarts." "gateway-tunnel"

  # MCP is a second deliberately narrow destination on the same closed
  # network. A healthy model tunnel does not prove this separate SSH key,
  # listener, alias, or forward. Probe it from the sandbox's actual network
  # before publishing the exact-SHA readiness marker.
  url="http://${MCP_HOSTNAME}:${MCP_TUNNEL_PORT}/healthz"
  log "probing ${url} from the ${network} network"
  probe_sandbox_relay "$network" "$url" "MCP-only relay" "$deadline" \
    "Check the mcp-tunnel SSH key, forward, and the control plane's MCP-only listener on port 8001." "mcp-tunnel"

  check_shared_live_stack "$deadline"
}

cmd_status() {
  compose ps
  printf '\n'
  compose logs --tail 20 \
    gateway-tunnel mcp-tunnel control-plane-tunnel \
    agent-egress-proxy workspace-broker runner
}

cmd_down() {
  compose down
  log "stopped. Jobs that were running were interrupted; they are retried when"
  log "their lease expires."
}

cmd_mark_ready() {
  complete_shared_live_marker "${1:-}"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  preflight) cmd_preflight ;;
  check) cmd_check 0 ;;
  status) cmd_status ;;
  down) cmd_down ;;
  mark-ready) shift; cmd_mark_ready "$@" ;;
  *) die "usage: $0 {up [N]|preflight|check|status|down|mark-ready SHA}" ;;
esac
