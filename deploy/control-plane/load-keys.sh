#!/bin/sh
# The control-plane image's entrypoint: fill each deployment key the
# environment leaves empty from the agent-keys volume, then run the command.
#
# The Compose `keys` service writes that volume before anything else starts
# (apps/backend/cloud_agent/deployment/deployment_keys.py). A value set in the
# environment wins, so a deployment that still sets one in its .env, or a
# command run with one, behaves exactly as before. A file this user cannot
# read, or a volume that is not mounted, leaves the variable as it was.
set -eu

keys=/run/cloud-agent/keys

# fill NAME CURRENT_VALUE
fill() {
  if [ -z "$2" ] && [ -r "${keys}/$1" ]; then
    export "$1=$(cat "${keys}/$1")"
  fi
}

fill AGENT_DATABASE_URL "${AGENT_DATABASE_URL:-}"
fill AGENT_LOCAL_SESSION_SECRET "${AGENT_LOCAL_SESSION_SECRET:-}"
fill AGENT_SETTINGS_ENCRYPTION_KEY "${AGENT_SETTINGS_ENCRYPTION_KEY:-}"
fill AGENT_SESSION_SECRET "${AGENT_SESSION_SECRET:-}"

exec "$@"
