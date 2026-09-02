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

# Which agent CLI versions this deployment bakes into its sandbox image.
#
# All optional, and unset is the normal case: an empty value passes no build
# argument at all, so the image is built from the reviewed default in
# `deploy/sandbox/Dockerfile.agent-sandbox` — byte for byte the image this host
# built before any of this existed. Setting one is how an environment runs a
# version other than the default (staging soaking a new CLI, or a deployment
# pinned back after a bad one) without editing the source both environments
# build from.
#
# Named exactly as the Dockerfile's `ARG`s — one name for one thing, so there
# is no mapping table between `.env` and `--build-arg` for a typo to hide in.
#
# The reads below are one spelled-out call each rather than a loop, because
# `test_env_examples.py` recognises configuration by exactly this literal
# `env_or_dotenv NAME` shape; a loop over a list of names would read to it as
# no configuration at all. `test_runtime_version_pins.py` then holds this set
# equal to the set of pins the Dockerfile declares, so a runtime added there
# without a line here fails rather than being quietly unconfigurable.
CLAUDE_CODE_VERSION="$(env_or_dotenv CLAUDE_CODE_VERSION)"
CODEX_VERSION="$(env_or_dotenv CODEX_VERSION)"
PI_VERSION="$(env_or_dotenv PI_VERSION)"
OPENCODE_VERSION="$(env_or_dotenv OPENCODE_VERSION)"
KILO_VERSION="$(env_or_dotenv KILO_VERSION)"

# Extra versions the image also carries, comma-separated, so an admin can switch
# this deployment between them from the admin page without a rebuild. Empty is
# the normal case and means the image holds exactly the pins above.
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
              AGENT_TUNNEL_SSH_DESTINATION AGENT_TUNNEL_SSH_KEY \
              AGENT_TUNNEL_SSH_KNOWN_HOSTS; do
    [[ -n "$(env_or_dotenv "$name")" ]] || missing+=("$name")
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    die "missing in .env: ${missing[*]} (see ENVIRONMENT.md)"
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
    local missing_roots=()
    for name in AGENT_HOST_STATE_ROOT; do
      [[ -n "$(env_or_dotenv "$name")" ]] || missing_roots+=("$name")
    done
    if [[ ${#missing_roots[@]} -gt 0 ]]; then
      die "this host runs another environment (SIBLING_COMPOSE_PROJECTS is set), so ${missing_roots[*]} must name a path of this stack's own; otherwise both stacks claim as whichever host enrolled first."
    fi
  fi

  local key known
  key="$(env_or_dotenv AGENT_TUNNEL_SSH_KEY)"
  known="$(env_or_dotenv AGENT_TUNNEL_SSH_KNOWN_HOSTS)"
  [[ -r "$key" ]] || die "AGENT_TUNNEL_SSH_KEY=$key is not readable on this host"
  [[ -s "$known" ]] || die \
    "AGENT_TUNNEL_SSH_KNOWN_HOSTS=$known is missing or empty. Create it with
     'ssh-keyscan <gateway-host> > $known' and check the fingerprint before
     trusting it — this file is what stops the enrolled-host credential going to
     whatever answers on that address."
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

  local slots runner_replicas resident_capacity
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
  local state_root
  state_root="$(env_or_dotenv AGENT_HOST_STATE_ROOT)"
  state_root="${state_root:-/var/lib/hybridinference/agent-host}"
  printf '%s' "${state_root%/}/shared-live-ready"
}

write_shared_live_marker() {
  local value="$1" ready_file ready_dir
  ready_file="$(shared_live_ready_file)"
  ready_dir="${ready_file%/*}"
  [[ "$ready_file" == /* && "$ready_dir" != "$ready_file" ]] \
    || die "shared-live ready marker must be an absolute file path (got '$ready_file')"
  mkdir -p "$ready_dir" \
    || die "could not create shared-live marker directory $ready_dir"
  # While this plain overwrite is empty or partial, runners omit the exact-SHA
  # capability. That makes an interrupted write fail closed.
  printf '%s\n' "$value" > "$ready_file" \
    || die "could not write shared-live marker $ready_file"
  chmod 0644 "$ready_file" \
    || die "could not make shared-live marker readable by runner replicas"
  AGENT_SHARED_LIVE_READY_FILE="$ready_file"
  export AGENT_SHARED_LIVE_READY_FILE
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

check_shared_live_stack() {
  # Ask every running replica to attest the exact image labels, helper pins,
  # runtime drivers, broker settings and slot count used by the claim path.
  # Inspecting only its environment would call a half-configured host ready —
  # the exact failure mode the shared-sandbox-v1 capability is meant to avoid.
  local runner_ids running_replicas expected_replicas id capability first_id=""
  runner_ids="$(compose ps -q runner)" \
    || die "could not list runner replicas for shared-live verification"
  [[ -n "$runner_ids" ]] || die "no running runner replica is available to attest shared-sandbox-v1"
  running_replicas="$(wc -w <<<"$runner_ids" | tr -d '[:space:]')"
  expected_replicas="$(env_or_dotenv AGENT_RUNNER_REPLICAS)"
  expected_replicas="${expected_replicas:-3}"
  [[ "$running_replicas" -ge "$expected_replicas" ]] \
    || die "only $running_replicas of $expected_replicas configured runner replicas are running; shared-live concurrency is not ready"
  for id in $runner_ids; do
    [[ -n "$first_id" ]] || first_id="$id"
    capability="$(docker exec "$id" python -c \
      'from cloud_agent_host.runner import _shared_live_host_capabilities as capabilities; from cloud_agent_host.sandbox import build_backend_from_env; backend = build_backend_from_env(); print(",".join(capabilities(backend, backend.version_stamp())))')" \
      || die "runner $id could not perform shared-live capability attestation"
    case ",${capability}," in
      *,shared-sandbox-v1,*) ;;
      *) die "runner $id did not attest shared-sandbox-v1 (reported '${capability:-no capability}'); check slots, runtime/helper pins, sandbox image labels and broker configuration" ;;
    esac
    case ",${capability}," in
      *,native-fork-at-anchor-v1,*) ;;
      *) die "runner $id did not attest native-fork-at-anchor-v1 (reported '${capability:-no capability}'); historical Fork/Edit & rewind is not deployment-ready" ;;
    esac
  done

  # Capability attestation proves the runner is configured to dial the broker;
  # make the dial as well. This catches a broker crash-loop (including a
  # DockerRootDir mismatch) before the control plane begins assigning chats.
  local broker_root expected_root control_status recent_runner_logs
  broker_root="$(docker exec "$first_id" python -c \
    'import json, os, urllib.request; url = os.environ["AGENT_WORKSPACE_BROKER_URL"].rstrip("/") + "/health"; request = urllib.request.Request(url, headers={"X-Agent-Workspace-Token": os.environ["AGENT_WORKSPACE_BROKER_TOKEN"]}); print(json.load(urllib.request.urlopen(request, timeout=10)).get("retained_docker_data_root") or "")')" \
    || die "runner $first_id cannot authenticate to the workspace broker health endpoint"
  expected_root="$(env_or_dotenv AGENT_RETAINED_DOCKER_DATA_ROOT)"
  expected_root="$(normalise_host_path "${expected_root:-/var/lib/docker}")"
  broker_root="$(normalise_host_path "$broker_root")"
  [[ "$broker_root" == "$expected_root" ]] \
    || die "workspace broker reports retained DockerRootDir '${broker_root:-unset}', expected '$expected_root'"

  # The inference-gateway probe above is a different tunnel. Prove the runner
  # can also reach the control plane that receives its capability heartbeat.
  # The control-plane deployment performs the authoritative second half: it
  # waits for a fresh database heartbeat and dials this broker in reverse.
  control_status="$(docker exec "$first_id" python -c \
    'import os, urllib.request; url = os.environ["AGENT_CONTROL_PLANE_URL"].rstrip("/") + "/healthz"; print(urllib.request.urlopen(url, timeout=10).status)')" \
    || die "runner $first_id cannot reach the control-plane health endpoint"
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
  prepare_shared_live_marker
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

  # Only the pins this deployment actually sets are passed; the rest fall
  # through to the Dockerfile's reviewed defaults. Logged one per line, because
  # "which CLI version did this host build" is the first question asked when a
  # job behaves differently here than elsewhere, and the build log is where
  # somebody looks before the database.
  local build_args=() name
  for name in CLAUDE_CODE_VERSION CODEX_VERSION PI_VERSION OPENCODE_VERSION KILO_VERSION \
              CLAUDE_CODE_EXTRA_VERSIONS CODEX_EXTRA_VERSIONS PI_EXTRA_VERSIONS \
              OPENCODE_EXTRA_VERSIONS KILO_EXTRA_VERSIONS; do
    [[ -n "${!name}" ]] || continue
    log "runtime pin: ${name}=${!name} (overrides the Dockerfile default)"
    build_args+=(--build-arg "${name}=${!name}")
  done

  log "building the sandbox image (${SANDBOX_IMAGE})"
  # The `+` guard is load-bearing under `set -u`: with no overrides at all,
  # a bare `"${build_args[@]}"` is an unbound reference on bash before 4.4,
  # and this script also runs on developer machines.
  docker build ${build_args[@]+"${build_args[@]}"} \
    -f deploy/sandbox/Dockerfile.agent-sandbox -t "$SANDBOX_IMAGE" .

  log "starting the gateway tunnel and ${replicas} runner(s)"
  # `--remove-orphans` because a renamed service leaves its old container
  # running otherwise: compose only replaces containers for services the file
  # still declares, so `agent-runner` outliving `runner` would mean two sets of
  # runners claiming from one host, which is the two-brokers incident again.
  AGENT_SANDBOX_IMAGE="$SANDBOX_IMAGE" AGENT_RUNNER_REPLICAS="$replicas" \
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
  validate_retained_config
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
    check_shared_live_stack
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

cmd_mark_ready() {
  complete_shared_live_marker "${1:-}"
}

case "${1:-}" in
  up) shift; cmd_up "$@" ;;
  preflight) cmd_preflight ;;
  check) cmd_check ;;
  status) cmd_status ;;
  down) cmd_down ;;
  mark-ready) shift; cmd_mark_ready "$@" ;;
  *) die "usage: $0 {up [N]|preflight|check|status|down|mark-ready SHA}" ;;
esac
