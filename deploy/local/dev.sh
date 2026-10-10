#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
ENV_FILE="$SCRIPT_DIR/.env.local"

if docker compose version >/dev/null 2>&1; then
  COMPOSE='docker compose'
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=docker-compose
else
  echo "Docker Compose is required (docker-compose or docker compose)." >&2
  exit 1
fi

compose() {
  configure_browser_urls
  LOCAL_SANDBOX_IMAGE=$(local_sandbox_image)
  LOCAL_HOST_SLOTS=$(local_host_slots)
  AGENT_DEPLOYMENT_SHA=$(local_deployment_sha)
  export LOCAL_SANDBOX_IMAGE LOCAL_HOST_SLOTS AGENT_DEPLOYMENT_SHA
  # Intentional word splitting: COMPOSE is either one command or `docker compose`.
  # shellcheck disable=SC2086
  $COMPOSE --env-file "$ENV_FILE" --project-name "$(local_project_name)" \
    -f "$REPO_ROOT/deploy/control-plane/docker-compose.yml" \
    -f "$SCRIPT_DIR/docker-compose.yml" "$@"
}

configure_browser_urls() {
  # These three URLs and the URL printed by this script must use one canonical
  # hostname. A host-only login-flow cookie set on 127.0.0.1 is not sent to a
  # callback on localhost (or vice versa), even though both reach the same
  # socket. Derive from the effective port on every invocation; explicit shell
  # values still win, while stale values in an older .env.local cannot split
  # the OAuth round trip or point a second stack back at the first one's port.
  edge_port=$(local_edge_port)
  GATEWAY_AUTHORIZE_URL=${GATEWAY_AUTHORIZE_URL:-http://127.0.0.1:${edge_port}/authorize}
  AGENT_SESSION_REDIRECT_URI=${AGENT_SESSION_REDIRECT_URI:-http://127.0.0.1:${edge_port}/api/v1/session/callback}
  AGENT_WEB_BASE_URL=${AGENT_WEB_BASE_URL:-http://127.0.0.1:${edge_port}}
  export GATEWAY_AUTHORIZE_URL AGENT_SESSION_REDIRECT_URI AGENT_WEB_BASE_URL
}

local_project_name() {
  project_name=${LOCAL_PROJECT_NAME:-}
  if [ -z "$project_name" ] && [ -f "$ENV_FILE" ]; then
    project_name=$(sed -n 's/^LOCAL_PROJECT_NAME=//p' "$ENV_FILE" | tail -n 1)
  fi
  printf '%s\n' "${project_name:-freeinference-local}"
}

local_sandbox_image() {
  image=$(env_file_value LOCAL_SANDBOX_IMAGE)
  if [ -n "$image" ]; then
    printf '%s\n' "$image"
    return
  fi
  project_slug=$(local_project_name | tr '[:upper:]_' '[:lower:]-' | tr -cd 'a-z0-9_.-')
  printf 'freeinference-cloud-agent-sandbox-%s:local\n' "${project_slug:-local}"
}

local_host_slots() {
  replicas=$(env_file_value LOCAL_HOST_SLOTS)
  replicas=${replicas:-2}
  case "$replicas" in
    '' | *[!0-9]*)
      echo "LOCAL_HOST_SLOTS must be an integer of at least 2." >&2
      return 1
      ;;
  esac
  if [ "$replicas" -lt 2 ]; then
    echo "LOCAL_HOST_SLOTS must be at least 2 for concurrent native forks." >&2
    return 1
  fi
  printf '%s\n' "$replicas"
}

local_deployment_sha() {
  deployment_sha=$(env_file_value AGENT_DEPLOYMENT_SHA)
  printf '%s\n' "${deployment_sha:-local-$(local_project_name)}"
}

local_control_network() {
  network_name=${AGENT_NETWORK_NAME:-}
  if [ -z "$network_name" ] && [ -f "$ENV_FILE" ]; then
    network_name=$(sed -n 's/^AGENT_NETWORK_NAME=//p' "$ENV_FILE" | tail -n 1)
  fi
  printf '%s\n' "${network_name:-cloud-agent}"
}

local_relay_network() {
  network_name=${LOCAL_RELAY_NETWORK:-}
  if [ -z "$network_name" ] && [ -f "$ENV_FILE" ]; then
    network_name=$(sed -n 's/^LOCAL_RELAY_NETWORK=//p' "$ENV_FILE" | tail -n 1)
  fi
  printf '%s\n' "${network_name:-freeinference-local-agent-relays}"
}

local_default_dns_server() {
  # The resolver this Docker engine already hands an ordinary container, which
  # is by construction one a container can reach: Colima's VM forwarder,
  # Docker Desktop's, or a Linux host's upstream behind systemd-resolved. A
  # loopback or IPv6 answer is not one a sandbox can use, and no answer at all
  # falls back to a public resolver.
  detected=$(docker run --rm --network bridge --entrypoint awk postgres:16-alpine \
    '/^nameserver [0-9]+[.][0-9]+[.][0-9]+[.][0-9]+$/ && $2 !~ /^127[.]/ { print $2; exit }' \
    /etc/resolv.conf 2>/dev/null) || detected=
  printf '%s\n' "${detected:-1.1.1.1}"
}

append_dns_server_if_missing() {
  # Detected once and written down, so it is visible, editable, and not a
  # container start on every invocation.
  if ! grep -q '^LOCAL_EGRESS_DNS_SERVER=' "$ENV_FILE"; then
    append_env_if_missing LOCAL_EGRESS_DNS_SERVER "$(local_default_dns_server)"
  fi
}

validate_network_owner() {
  network_name=$1
  expected_project=$2
  if ! docker network inspect "$network_name" >/dev/null 2>&1; then
    return
  fi
  owner=$(docker network inspect "$network_name" \
    --format '{{ index .Labels "com.docker.compose.project" }}' 2>/dev/null) || {
    echo "Could not inspect existing Docker network '$network_name'." >&2
    return 1
  }
  if [ "$owner" != "$expected_project" ]; then
    echo "Docker network '$network_name' belongs to Compose project '${owner:-<unlabelled>}', not '$expected_project'." >&2
    echo "Choose unique LOCAL_PROJECT_NAME, AGENT_NETWORK_NAME, LOCAL_RELAY_NETWORK, LOCAL_CHAT_NETWORK_POOL, and published ports before starting this checkout." >&2
    return 1
  fi
}

validate_local_network_ownership() {
  project_name=$(local_project_name)
  control_network=$(local_control_network)
  relay_network=$(local_relay_network)
  if [ "$control_network" = "$relay_network" ]; then
    echo "AGENT_NETWORK_NAME and LOCAL_RELAY_NETWORK must be different." >&2
    return 1
  fi
  validate_network_owner "$control_network" "$project_name"
  validate_network_owner "$relay_network" "$project_name"
}

random_hex() {
  openssl rand -hex 32
}

append_env_if_missing() {
  name=$1
  value=$2
  if ! grep -q "^${name}=" "$ENV_FILE"; then
    printf '%s=%s\n' "$name" "$value" >>"$ENV_FILE"
  fi
}

local_postgres_volumes() {
  project_name=$(local_project_name)
  docker volume ls --quiet \
    --filter "label=com.docker.compose.project=$project_name" \
    --filter "label=com.docker.compose.volume=agent-postgres"
}

refuse_lost_database_credentials() {
  volumes=$(local_postgres_volumes) || {
    echo "Could not inspect Docker volumes before generating $ENV_FILE." >&2
    echo "Start Docker, then retry; credentials are never regenerated while retained database state may exist." >&2
    return 1
  }
  if [ -n "$volumes" ]; then
    echo "$ENV_FILE is missing, but Compose project '$(local_project_name)' still owns its Postgres volume:" >&2
    printf '%s\n' "$volumes" >&2
    echo "Restore the original .env.local (especially AGENT_POSTGRES_PASSWORD and AGENT_DATABASE_URL) and retry." >&2
    echo "This script will not delete the volume or invent a new password that cannot authenticate to it." >&2
    return 1
  fi
}

upgrade_existing_env() {
  # Keep every existing credential and operator-selected port.  Local env
  # files predate both retained-only deployment and the server-side OAuth
  # round trip; simply returning when one exists makes an otherwise valid
  # checkout fail Compose interpolation after an update.
  edge_port=$(sed -n 's/^LOCAL_EDGE_PORT=//p' "$ENV_FILE" | tail -n 1)
  edge_port=${edge_port:-13002}
  append_env_if_missing AGENT_WORKSPACE_BROKER_TOKEN "$(random_hex)"
  append_env_if_missing LOCAL_PROJECT_NAME freeinference-local
  append_env_if_missing LOCAL_SANDBOX_IMAGE "$(local_sandbox_image)"
  append_env_if_missing LOCAL_HOST_SLOTS 2
  append_env_if_missing AGENT_DEPLOYMENT_SHA "local-$(local_project_name)"
  append_env_if_missing LOCAL_RELAY_NETWORK freeinference-local-agent-relays
  append_env_if_missing LOCAL_CHAT_NETWORK_POOL 10.240.0.0/16
  append_dns_server_if_missing
  append_env_if_missing GATEWAY_ISSUER "freeinference-local"
  append_env_if_missing GATEWAY_AUTHORIZE_URL "http://127.0.0.1:${edge_port}/authorize"
  append_env_if_missing AGENT_SESSION_REDIRECT_URI \
    "http://127.0.0.1:${edge_port}/api/v1/session/callback"
  append_env_if_missing AGENT_WEB_BASE_URL "http://127.0.0.1:${edge_port}"
  append_env_if_missing AGENT_WEB_GATEWAY_URL ""
}

ensure_env() {
  if [ -f "$ENV_FILE" ]; then
    umask 077
    upgrade_existing_env
    return
  fi
  refuse_lost_database_credentials
  umask 077
  postgres_password=$(random_hex)
  dispatch_token=$(random_hex)
  enroll_token=$(random_hex)
  enroll_hash=$(printf '%s' "$enroll_token" | openssl dgst -sha256 -r | awk '{print $1}')
  session_secret=$(random_hex)
  control_secret=$(random_hex)
  source_secret=$(random_hex)
  broker_token=$(random_hex)

  {
    printf '%s\n' \
      '# Generated by deploy/local/dev.sh. Local-only; never commit this file.' \
      'AGENT_POSTGRES_USER=cloudagent' \
      'AGENT_POSTGRES_DB=cloudagent' \
      "AGENT_POSTGRES_PASSWORD=$postgres_password" \
      "AGENT_DATABASE_URL=postgresql://cloudagent:$postgres_password@postgres:5432/cloudagent" \
      'GATEWAY_BASE_URL=http://local-gateway:8080' \
      'GATEWAY_ISSUER=freeinference-local' \
      'GATEWAY_JWKS_URL=http://local-gateway:8080/v1/identity/jwks' \
      "GATEWAY_GRANT_DISPATCH_TOKEN=$dispatch_token" \
      "AGENT_SESSION_SECRET=$session_secret" \
      "AGENT_CONTROL_TOKEN_SECRET=$control_secret" \
      "AGENT_SOURCE_CONTROL_ENCRYPTION_KEY=$source_secret" \
      "AGENT_WORKSPACE_BROKER_TOKEN=$broker_token" \
      "LOCAL_ENROLL_TOKEN=$enroll_token" \
      "LOCAL_ENROLL_TOKEN_SHA256=$enroll_hash" \
      'AGENT_SESSION_COOKIE_SECURE=false' \
      'AGENT_REPO_ALLOWLIST=local/demo' \
      'AGENT_BROKER_URL_ALLOWED_HOSTS=workspace-broker' \
      'AGENT_NETWORK_NAME=freeinference-local-control' \
      'AGENT_CONTROL_PLANE_BIND=127.0.0.1' \
      'AGENT_CONTROL_PLANE_PORT=18000' \
      'AGENT_MCP_RELAY_BIND=127.0.0.1' \
      'AGENT_MCP_RELAY_PORT=18001' \
      'AGENT_WEB_BIND=127.0.0.1' \
      'AGENT_WEB_PORT=13003' \
      'LOCAL_EDGE_PORT=13002' \
      'LOCAL_PROJECT_NAME=freeinference-local' \
      'LOCAL_SANDBOX_IMAGE=freeinference-cloud-agent-sandbox-freeinference-local:local' \
      'LOCAL_HOST_SLOTS=2' \
      'AGENT_DEPLOYMENT_SHA=local-freeinference-local' \
      'LOCAL_RELAY_NETWORK=freeinference-local-agent-relays' \
      'LOCAL_CHAT_NETWORK_POOL=10.240.0.0/16' \
      'GATEWAY_AUTHORIZE_URL=http://127.0.0.1:13002/authorize' \
      'AGENT_SESSION_REDIRECT_URI=http://127.0.0.1:13002/api/v1/session/callback' \
      'AGENT_WEB_BASE_URL=http://127.0.0.1:13002' \
      'AGENT_WEB_BASE_PATH=' \
      'AGENT_WEB_API_URL=/api' \
      'AGENT_WEB_GATEWAY_URL=' \
      'AGENT_WEB_APP_NAME=FreeInference Cloud Agent (local)' \
      'LOCAL_CHAT_IDLE_TIMEOUT_S=15' \
      'LOCAL_AGENT_TIMEOUT_S=600' \
      'AGENT_RETAINED_DOCKER_DATA_ROOT=/var/lib/docker'
  } >"$ENV_FILE"
  append_dns_server_if_missing
  echo "Generated $ENV_FILE with local-only random credentials."
}

env_file_value() {
  name=$1
  value=$(printenv "$name" 2>/dev/null || true)
  if [ -z "$value" ] && [ -f "$ENV_FILE" ]; then
    value=$(sed -n "s/^${name}=//p" "$ENV_FILE" | tail -n 1)
  fi
  printf '%s' "$value"
}

require_local_database_credentials() {
  missing=
  for name in AGENT_POSTGRES_PASSWORD AGENT_DATABASE_URL; do
    if [ -z "$(env_file_value "$name")" ]; then
      missing="$missing $name"
    fi
  done
  if [ -n "$missing" ]; then
    echo "$ENV_FILE is missing required database credential(s):$missing" >&2
    echo "Restore them from the original local environment; this script will not reset the retained Postgres volume." >&2
    return 1
  fi
}

start_database_and_probe() {
  echo "Starting Postgres before the rest of the local stack."
  compose up --detach postgres
  attempts=0
  until compose exec -T postgres pg_isready >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 60 ]; then
      echo "Local Postgres did not become ready; inspect it with '$0 logs postgres'." >&2
      return 1
    fi
    sleep 2
  done

  echo "Verifying the retained database password from $ENV_FILE."
  if ! compose run --rm --no-deps migrate python -c \
    'import asyncio, os, asyncpg
async def probe():
    connection = await asyncpg.connect(os.environ["AGENT_DATABASE_URL"], timeout=10)
    try:
        await connection.fetchval("SELECT 1")
    finally:
        await connection.close()
asyncio.run(probe())'; then
    echo "The configured AGENT_DATABASE_URL cannot authenticate to the existing Postgres volume." >&2
    echo "Restore the original AGENT_POSTGRES_PASSWORD/AGENT_DATABASE_URL. No volume was deleted or password reset." >&2
    return 1
  fi

  echo "Applying the agent-owned database migrations before full startup."
  compose run --rm --no-deps migrate
}

local_edge_port() {
  edge_port=${LOCAL_EDGE_PORT:-}
  if [ -z "$edge_port" ]; then
    edge_port=$(sed -n 's/^LOCAL_EDGE_PORT=//p' "$ENV_FILE" | tail -n 1)
  fi
  case "$edge_port" in
    '' | *[!0-9]*)
      echo "LOCAL_EDGE_PORT must be an integer from 1 to 65535." >&2
      return 1
      ;;
  esac
  if [ "$edge_port" -lt 1 ] || [ "$edge_port" -gt 65535 ]; then
    echo "LOCAL_EDGE_PORT must be an integer from 1 to 65535." >&2
    return 1
  fi
  printf '%s\n' "$edge_port"
}

local_base_url() {
  printf 'http://127.0.0.1:%s\n' "$(local_edge_port)"
}

wait_ready() {
  base_url=$(local_base_url)
  expected_stack=$(local_project_name)
  attempts=0
  until health=$(curl --fail --silent --show-error "$base_url/health") \
    && actual_stack=$(printf '%s' "$health" | python3 -c \
      'import json, sys; print(json.load(sys.stdin).get("stack_id", ""))') \
    && [ "$actual_stack" = "$expected_stack" ] \
    && curl --fail --silent --show-error "$base_url/api/healthz" >/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 90 ]; then
      echo "Local stack '$expected_stack' did not become ready at $base_url." >&2
      if [ "${actual_stack:-}" ]; then
        echo "That port answered as a different stack: '$actual_stack'." >&2
      fi
      echo "Recent logs:" >&2
      compose logs --tail=120 >&2
      return 1
    fi
    sleep 2
  done
}

command=${1:-up}
case "$command" in
  build)
    ensure_env
    shift
    compose build "$@"
    ;;
  up)
    ensure_env
    require_local_database_credentials
    validate_local_network_ownership
    shift
    if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != --no-build ]; }; then
      echo "Usage: $0 up [--no-build]" >&2
      exit 1
    fi
    build_flag=--build
    if [ "${1:-}" = --no-build ]; then
      build_flag=--no-build
    else
      compose build sandbox-image migrate
    fi
    start_database_and_probe
    compose up --detach "$build_flag"
    wait_ready
    echo "Local Cloud Agent is ready: $(local_base_url)"
    echo "Run '$0 smoke' for an automated login/config/agent lifecycle check."
    ;;
  start)
    ensure_env
    require_local_database_credentials
    validate_local_network_ownership
    start_database_and_probe
    compose start
    wait_ready
    echo "Local Cloud Agent is ready: $(local_base_url)"
    ;;
  stop)
    ensure_env
    compose stop
    ;;
  status|ps)
    ensure_env
    compose ps
    ;;
  logs)
    ensure_env
    shift || true
    compose logs --follow --tail=150 "$@"
    ;;
  smoke)
    ensure_env
    validate_local_network_ownership
    wait_ready
    exec python3 "$SCRIPT_DIR/smoke.py" "${2:-$(local_base_url)}"
    ;;
  config)
    ensure_env
    compose config --quiet
    echo "Local Compose configuration is valid."
    ;;
  *)
    echo "Usage: $0 {build [compose build options/services]|up|start|stop|status|logs [service]|smoke|config}" >&2
    exit 2
    ;;
esac
