#!/usr/bin/env bash
# Shared by the two deployment entry points and direct host commands.

ARTIFACT_RECIPE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

artifact_env_value() {
  local key="$1" line
  while IFS= read -r line; do
    if [[ "${line%%=*}" == "$key" ]]; then
      printf '%s' "${line#*=}"
      return 0
    fi
  done <<< "${_ARTIFACT_COMPOSE_ENVIRONMENT}"
}

prepare_artifact_deployment() {
  local stack="$1" requested_sha="${SHA:-}" variant="" base_path line key temporary source_compose
  local python="${AGENT_DEPLOY_PYTHON:-python3}"
  local compose_command=(docker compose --project-directory "$PWD")
  local arguments=() render_arguments=()

  [[ -n "${AGENT_RELEASE_MANIFEST:-}" ]] \
    || die "artifact mode requires AGENT_RELEASE_MANIFEST"
  [[ -f "$AGENT_RELEASE_MANIFEST" ]] \
    || die "release manifest does not exist: $AGENT_RELEASE_MANIFEST"
  [[ "$(cat "$ARTIFACT_RECIPE_ROOT/deploy/DEPLOY_PROTOCOL")" == "shared-live-exact-sha-v4" ]] \
    || die "this recipe does not implement shared-live-exact-sha-v4"
  AGENT_RELEASE_MANIFEST="$(cd "$(dirname "$AGENT_RELEASE_MANIFEST")" && pwd)/$(basename "$AGENT_RELEASE_MANIFEST")"
  export AGENT_RELEASE_MANIFEST

  if [[ -n "${2:-}" ]]; then
    _ARTIFACT_COMPOSE_ENVIRONMENT="$2"
  else
    [[ ! -f .env ]] || compose_command+=(--env-file .env)
    _ARTIFACT_COMPOSE_ENVIRONMENT="$(
      printf 'services: {}\n' | "${compose_command[@]}" -f - config --environment
    )" || die "Compose could not resolve the deployment environment"
  fi

  if [[ "$stack" == "control-plane" ]]; then
    variant="$(artifact_env_value AGENT_WEB_VARIANT)"
    if [[ -z "$variant" ]]; then
      base_path="$(artifact_env_value AGENT_WEB_BASE_PATH)"
      case "$base_path" in
        "") variant=root ;;
        /agents) variant=agents ;;
        *) die "AGENT_WEB_BASE_PATH must select the root or agents web variant" ;;
      esac
    fi
    arguments+=(--variant "$variant")
    render_arguments+=(--variant "$variant")
  fi

  # Only release-owned values are passed on argv. The resolved environment also
  # contains credentials, which must never become process arguments or logs.
  while IFS= read -r line; do
    key="${line%%=*}"
    case "$key" in
      AGENT_DEPLOYMENT_SHA|AGENT_CONTROL_IMAGE|AGENT_WEB_IMAGE|AGENT_WEB_VARIANT|AGENT_WEB_BASE_PATH|AGENT_RUNNER_IMAGE|AGENT_TUNNEL_IMAGE|AGENT_SANDBOX_IMAGE)
        arguments+=(--ambient "$line") ;;
    esac
  done <<< "$_ARTIFACT_COMPOSE_ENVIRONMENT"

  temporary="$(mktemp -d "${TMPDIR:-/tmp}/agent-release-env.XXXXXX")"
  # Both helpers consume the same bytes even if the caller refreshes its file.
  if ! cp "$AGENT_RELEASE_MANIFEST" "$temporary/release.json"; then
    rm -rf "$temporary"
    return 1
  fi
  if ! "$python" "$ARTIFACT_RECIPE_ROOT/deploy/release/release_env.py" \
    --manifest "$temporary/release.json" --stack "$stack" \
    ${arguments[@]+"${arguments[@]}"} --output "$temporary/release.env"; then
    rm -rf "$temporary"
    return 1
  fi
  source "$temporary/release.env"
  SHA="$AGENT_DEPLOYMENT_SHA"
  if [[ -n "$requested_sha" && "$requested_sha" != "$SHA" ]]; then
    rm -rf "$temporary"
    die "requested commit $requested_sha differs from release commit $SHA"
  fi

  mkdir -p "$PWD/deploy/$stack"
  case "$stack" in
    control-plane)
      COMPOSE_FILE="$PWD/deploy/control-plane/.release-${SHA}-${variant}.yml"
      source_compose="$ARTIFACT_RECIPE_ROOT/deploy/control-plane/docker-compose.yml"
      AGENT_EDGE_CONFIG_FILE="$ARTIFACT_RECIPE_ROOT/deploy/control-plane/edge.nginx.conf"
      export AGENT_EDGE_CONFIG_FILE
      # Generated Compose lives in DEPLOY_ROOT; the bundled definitions live
      # in the selected recipe. Preserve an operator-owned override.
      AGENT_PGADMIN_SERVERS_FILE="$(artifact_env_value AGENT_PGADMIN_SERVERS_FILE)"
      AGENT_PGADMIN_SERVERS_FILE="${AGENT_PGADMIN_SERVERS_FILE:-$ARTIFACT_RECIPE_ROOT/deploy/control-plane/pgadmin-servers.json}"
      export AGENT_PGADMIN_SERVERS_FILE
      AGENT_PGADMIN_ENTRYPOINT_FILE="$(artifact_env_value AGENT_PGADMIN_ENTRYPOINT_FILE)"
      AGENT_PGADMIN_ENTRYPOINT_FILE="${AGENT_PGADMIN_ENTRYPOINT_FILE:-$ARTIFACT_RECIPE_ROOT/deploy/control-plane/pgadmin-entrypoint.py}"
      export AGENT_PGADMIN_ENTRYPOINT_FILE
      ;;
    host)
      COMPOSE_FILE="$PWD/deploy/host/.release-${SHA}.yml"
      source_compose="$ARTIFACT_RECIPE_ROOT/deploy/host/docker-compose.sandbox.yml"
      ;;
    *) die "unknown artifact deployment stack: $stack" ;;
  esac
  if ! "$python" "$ARTIFACT_RECIPE_ROOT/deploy/release/artifact_compose.py" \
    --manifest "$temporary/release.json" --compose "$source_compose" \
    --stack "$stack" ${render_arguments[@]+"${render_arguments[@]}"} --output "$COMPOSE_FILE"; then
    rm -rf "$temporary"
    return 1
  fi
  rm -rf "$temporary"
}
