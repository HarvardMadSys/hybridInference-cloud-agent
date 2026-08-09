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
# The piece that makes a remote host work is a tunnel container on both the
# closed agent network and a routable one, forwarding a single port to the
# gateway host over ssh. The sandbox's world stays what it always was: one
# endpoint, ours. Nothing here is reachable from the sandbox except that.
#
# Usage, from the repository root on the runner host:
#
#   deploy/host/sandbox_host.sh up [N]   # build, start the tunnel + N runners
#   deploy/host/sandbox_host.sh status   # what is running, and the log tails
#   deploy/host/sandbox_host.sh check    # prove the tunnel reaches the gateway
#   deploy/host/sandbox_host.sh down     # stop everything this file started
#
# Required in .env on this host:
#
#   COMPOSE_PROJECT                this stack's compose project, unique on this machine
#   AGENT_DISPATCHER_TOKEN         same value as on the gateway
#   AGENT_SANDBOX_IMAGE            image the sandboxes run
#   AGENT_TUNNEL_SSH_DESTINATION   user@gateway-host
#   AGENT_TUNNEL_SSH_KEY           path to this host's tunnel key (mounted ro)
#   AGENT_TUNNEL_SSH_KNOWN_HOSTS   path to a known_hosts holding the gateway's key
#
# Restrict the tunnel key on the gateway host — it needs one forwarded port and
# no shell:
#
#   command="",restrict,port-forwarding,permitopen="127.0.0.1:8080" ssh-ed25519 AAAA... agent-tunnel

set -Eeuo pipefail

APP_DIR="${APP_DIR:-$(pwd)}"
cd "$APP_DIR"

log() { printf '[sandbox-host] %s\n' "$*"; }
die() { printf '[sandbox-host] ERROR: %s\n' "$*" >&2; exit 1; }

env_or_dotenv() {
  # A value may come from the environment or from .env; compose reads both, so
  # the preflight has to look in both or it rejects working configurations.
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
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
  [[ -n "$COMPOSE_PROJECT" ]] || die \
    "COMPOSE_PROJECT is not set — put it in .env (e.g. freeinference-cloud-agent-sandbox-staging). It is what keeps this stack apart from the other environment on this machine."
  docker compose \
    -p "$COMPOSE_PROJECT" \
    -f deploy/host/docker-compose.sandbox.yml \
    --env-file .env "$@"
}

SANDBOX_IMAGE="$(env_or_dotenv AGENT_SANDBOX_IMAGE)"
SANDBOX_IMAGE="${SANDBOX_IMAGE:-freeinference-cloud-agent-job:latest}"
GATEWAY_HOSTNAME="$(env_or_dotenv AGENT_GATEWAY_HOSTNAME)"
GATEWAY_HOSTNAME="${GATEWAY_HOSTNAME:-agent-gateway}"
TUNNEL_PORT="$(env_or_dotenv AGENT_TUNNEL_LISTEN_PORT)"
TUNNEL_PORT="${TUNNEL_PORT:-8080}"
EGRESS_NETWORK="$(env_or_dotenv AGENT_EGRESS_NETWORK_PLATFORM_ONLY)"
EGRESS_NETWORK="${EGRESS_NETWORK:-agent-egress}"

require_env() {
  # Named up front, because each of these fails much later and much less
  # legibly: a missing token as a 401 per claim, a missing key as a container
  # that restarts forever, a missing known_hosts as a refusal nobody reads.
  local missing=() name
  for name in AGENT_DISPATCHER_TOKEN AGENT_SANDBOX_IMAGE \
              AGENT_TUNNEL_SSH_DESTINATION AGENT_TUNNEL_SSH_KEY \
              AGENT_TUNNEL_SSH_KNOWN_HOSTS; do
    [[ -n "$(env_or_dotenv "$name")" ]] || missing+=("$name")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "missing in .env: ${missing[*]} (see ENVIRONMENT.md)"
  fi

  # On a machine running two environments, the three state roots must be said
  # out loud. They are host paths bind-mounted into the containers, and they
  # default to the same three directories for every stack — so two stacks that
  # accept the defaults do not merely sit beside each other, they share:
  #
  #   AGENT_HOST_STATE_ROOT   the enrolment credential. The control plane reads
  #                           the host *from the credential*, not from the claim
  #                           body, so the second stack registers as the first
  #                           one — `AGENT_RUNNER_HOST` cannot correct it.
  #   AGENT_WORKDIR_ROOT      every job's checkout, patch and workspace.
  #   AGENT_SNAPSHOT_ROOT     the same, kept for a week.
  #
  # Required only when a sibling is named, because that is exactly when the
  # defaults stop being safe — a single-stack host has nothing to collide with
  # and should not have to configure paths it does not care about.
  if [[ -n "$(env_or_dotenv SIBLING_COMPOSE_PROJECTS)" ]]; then
    local shared=() path
    for name in AGENT_HOST_STATE_ROOT AGENT_WORKDIR_ROOT AGENT_SNAPSHOT_ROOT; do
      [[ -n "$(env_or_dotenv "$name")" ]] || shared+=("$name")
    done
    if [[ ${#shared[@]} -gt 0 ]]; then
      die "this host runs another environment (SIBLING_COMPOSE_PROJECTS is set), so ${shared[*]} must name paths of this stack's own — unset they default to /var/lib/hybridinference/*, which both stacks would then share: one environment's jobs reading the other's workspaces, and both claiming as whichever host enrolled first."
    fi
  fi

  local key known
  key="$(env_or_dotenv AGENT_TUNNEL_SSH_KEY)"
  known="$(env_or_dotenv AGENT_TUNNEL_SSH_KNOWN_HOSTS)"
  [[ -r "$key" ]] || die "AGENT_TUNNEL_SSH_KEY=$key is not readable on this host"
  [[ -s "$known" ]] || die \
    "AGENT_TUNNEL_SSH_KNOWN_HOSTS=$known is missing or empty. Create it with
     'ssh-keyscan <gateway-host> > $known' and check the fingerprint before
     trusting it — this file is what stops the dispatcher credential going to
     whatever answers on that address."
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
  require_kata_host
  kata_smoke_test
}

cmd_up() {
  local replicas="${1:-$(env_or_dotenv AGENT_RUNNER_REPLICAS)}"
  replicas="${replicas:-3}"
  [[ "$replicas" =~ ^[0-9]+$ ]] || die "replicas must be a number, got '$replicas'"
  command -v docker >/dev/null || die "docker is not installed on this host"
  docker info >/dev/null 2>&1 || die "the Docker daemon is not running (or not accessible)"
  require_env
  # Before the build, not after: a host that cannot isolate should not spend
  # ten minutes building an image it must not run.
  require_kata_host
  kata_smoke_test

  # This machine's name in the admin host switch. Resolved here because the
  # script runs on the host: inside a container the hostname is a container id,
  # so a runner cannot answer "which machine am I" for itself.
  if [[ -z "$(env_or_dotenv AGENT_RUNNER_HOST)" ]]; then
    AGENT_RUNNER_HOST="$(hostname -s 2>/dev/null || hostname)"
    export AGENT_RUNNER_HOST
    log "this machine joins the host pool as '${AGENT_RUNNER_HOST}'" \
        "(set AGENT_RUNNER_HOST in .env to rename it)"
  fi

  log "building the sandbox image (${SANDBOX_IMAGE})"
  docker build -f deploy/sandbox/Dockerfile.agent-sandbox -t "$SANDBOX_IMAGE" .

  log "starting the gateway tunnel and ${replicas} runner(s)"
  AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" AGENT_RUNNER_REPLICAS="$replicas" \
    # `--remove-orphans` because a renamed service leaves its old container
  # running otherwise: compose only replaces containers for services the file
  # still declares, so `agent-runner` outliving `runner` would mean two sets of
  # runners claiming from one host, which is the two-brokers incident again.
  compose up -d --build --remove-orphans

  # The runner's preflight now makes a real request to the gateway through the
  # tunnel, so a tunnel that came up without a working upstream fails here
  # rather than one job at a time. Give it a moment and surface the verdict.
  sleep 5
  # In a subshell, because `die` calls `exit` and from a function that ends the
  # whole script — the logs below are exactly what explains a failed probe, so
  # they must still print. But the verdict is kept: swallowing it printed the
  # tunnel diagnosis, then "done", and an operator reads the last line.
  local probe_ok=0
  ( cmd_check ) || probe_ok=$?
  compose logs --tail 20 runner
  if [[ "$probe_ok" -ne 0 ]]; then
    die "the runners are up but a sandbox cannot reach the gateway through the
     tunnel (see the probe output above). Jobs will claim and then fail at
     their first model call. Fix the tunnel, then re-run 'check'."
  fi
  log "done. 'deploy/host/sandbox_host.sh status' shows the live tail."
}

cmd_check() {
  # Ask from inside the closed network, which is the only vantage point whose
  # answer means anything: reaching the gateway from this host's shell proves
  # nothing about what a sandbox can reach.
  local network="$EGRESS_NETWORK"
  local url="http://${GATEWAY_HOSTNAME}:${TUNNEL_PORT}/health"
  log "probing ${url} from the ${network} network"
  if docker run --rm --network "$network" --entrypoint /bin/sh "$SANDBOX_IMAGE" \
       -c "curl -sS -o /dev/null -w '%{http_code}' --max-time 15 '$url'" 2>/dev/null; then
    printf '\n'
    log "the sandbox network can reach the gateway through the tunnel"
    return 0
  fi
  printf '\n'
  die "a sandbox on ${network} cannot reach ${url}.
     Check 'docker logs' for the gateway-tunnel container: an ssh that
     cannot authenticate or cannot bind its forward exits, and the container
     restarts. Resolving the name is not enough — the forward has to be up."
}

cmd_status() {
  compose ps
  printf '\n'
  compose logs --tail 20 gateway-tunnel runner
}

cmd_down() {
  compose down
  log "stopped. Jobs that were running were interrupted; they are retried when"
  log "their lease expires."
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  preflight) cmd_preflight ;;
  check) cmd_check ;;
  status) cmd_status ;;
  down) cmd_down ;;
  *) die "usage: $0 {up [N]|preflight|check|status|down}" ;;
esac
