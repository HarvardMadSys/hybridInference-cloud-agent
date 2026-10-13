#!/usr/bin/env bash
# Run the standard backend, PostgreSQL, and frontend CI gates from any checkout.
set -euo pipefail

usage() {
  echo "Usage: $0 [all|backend|database|frontend]"
  echo "Requires uv and Python 3.12; frontend requires Node 22/npm; database requires Docker."
}

if [[ $# -gt 1 ]]; then
  usage >&2
  exit 2
fi
CI_TIER=${1:-all}
case "$CI_TIER" in
  all|backend|database|frontend) ;;
  -h|--help) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Local CI requires $1 on PATH" >&2
    exit 1
  fi
}

if [[ "$CI_TIER" != frontend ]]; then
  require_command uv
fi
if [[ "$CI_TIER" == all || "$CI_TIER" == database ]]; then
  require_command docker
fi
if [[ "$CI_TIER" == all || "$CI_TIER" == frontend ]]; then
  require_command node
  require_command npm
  if [[ $(node -p 'process.versions.node.split(".")[0]') != 22 ]]; then
    echo "Use Node.js 22 to match CI" >&2
    exit 1
  fi
fi

CI_REPO=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$CI_REPO"
# Match CI rather than the restrictive umask of an enclosing agent process.
umask 022
export NEXT_TELEMETRY_DISABLED=1
# Sandbox version checks use the repository's pins, not an enclosing agent's CLI.
unset CODEX_VERSION

if [[ -z ${CI_LOG_DIR:-} ]]; then
  mkdir -p "$CI_REPO/.local-ci"
  CI_LOG_DIR=$(mktemp -d "$CI_REPO/.local-ci/${CI_TIER}.XXXXXX")
else
  mkdir -p "$CI_LOG_DIR"
  CI_LOG_DIR=$(cd -- "$CI_LOG_DIR" && pwd)
fi
printf 'Local CI: %s\nLogs: %s\n' "$CI_TIER" "$CI_LOG_DIR"
CI_FAILED=0
CI_RESULTS=()
CI_DATABASE_ID=""

cleanup() {
  if [[ -n "$CI_DATABASE_ID" ]]; then
    docker rm -fv "$CI_DATABASE_ID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

run_check() {
  local name=$1
  shift
  printf '\nRunning %s\n' "$name"
  if "$@" 2>&1 | tee "$CI_LOG_DIR/$name.log"; then
    CI_RESULTS+=("PASS $name")
  else
    CI_RESULTS+=("FAIL $name")
    CI_FAILED=1
  fi
}

if [[ "$CI_TIER" != frontend ]]; then
  uv sync --frozen --python 3.12
fi

if [[ "$CI_TIER" == all || "$CI_TIER" == backend ]]; then
  run_check ruff-format uv run --frozen ruff format --check .
  run_check ruff-lint uv run --frozen ruff check --no-fix .
  run_check pydocstyle uv run --frozen pydocstyle apps/backend/cloud_agent apps/backend/cloud_agent_host apps/backend/cloud_agent_shared
  run_check mypy uv run --frozen mypy
  run_check pytest uv run --frozen pytest -n 4 --dist loadfile -q -m 'not external and not dbtest'
fi

if [[ "$CI_TIER" == all || "$CI_TIER" == database ]]; then
  # CI gets a fresh cluster per run. Startup tests also use the base database,
  # so reusing even dedicated test databases leaks settings between runs.
  export TEST_DB_HOST=127.0.0.1
  export TEST_DB_NAME=hybridinference_test_db
  export TEST_DB_USER=postgres
  export TEST_DB_PASSWORD=postgres
  CI_DATABASE_ID=$(docker run --rm -d \
    --name "cloud-agent-local-ci-$$-${RANDOM}" \
    -p 127.0.0.1::5432 \
    -e POSTGRES_USER="$TEST_DB_USER" \
    -e POSTGRES_PASSWORD="$TEST_DB_PASSWORD" \
    -e POSTGRES_DB="$TEST_DB_NAME" \
    postgres:15 -c fsync=off)
  CI_DATABASE_READY=false
  for ((attempt=0; attempt<30; attempt++)); do
    # The temporary init server accepts Unix sockets before TCP is ready.
    if docker exec "$CI_DATABASE_ID" pg_isready -h 127.0.0.1 -U "$TEST_DB_USER" -d "$TEST_DB_NAME" >/dev/null 2>&1; then
      CI_DATABASE_READY=true
      break
    fi
    sleep 1
  done
  if [[ "$CI_DATABASE_READY" != true ]]; then
    docker logs "$CI_DATABASE_ID" >&2
    echo "PostgreSQL did not become ready" >&2
    exit 1
  fi
  CI_DATABASE_MAPPING=$(docker port "$CI_DATABASE_ID" 5432/tcp)
  export TEST_DB_PORT=${CI_DATABASE_MAPPING##*:}
  uv run --frozen python - <<'PY'
import asyncio
import os

import asyncpg


async def main():
    name = os.environ["TEST_DB_NAME"]
    if "_test_" not in name:
        raise SystemExit("Refusing to run database tests without a dedicated _test_ database")
    for database in [name, *(f"{name}_gw{i}" for i in range(4))]:
        conn = await asyncpg.connect(
            host=os.environ["TEST_DB_HOST"],
            port=int(os.environ["TEST_DB_PORT"]),
            database=database,
            user=os.environ["TEST_DB_USER"],
            password=os.environ["TEST_DB_PASSWORD"],
            timeout=10,
        )
        try:
            if await conn.fetchval("SELECT 1") != 1:
                raise SystemExit(f"Cannot query test database {database}")
            if database == name:
                if await conn.fetchval("SHOW fsync") != "off":
                    raise SystemExit("The disposable test container is missing CI's fsync setting")
                for index in range(4):
                    await conn.execute(f'CREATE DATABASE "{name}_gw{index}"')
        finally:
            await conn.close()
    print("PostgreSQL: base and all four worker databases reachable")


asyncio.run(main())
PY
  rm -f "$CI_LOG_DIR/dbtest.xml"
  run_check dbtest uv run --frozen pytest -q -n 4 --dist loadfile -m dbtest --junitxml="$CI_LOG_DIR/dbtest.xml"
  run_check dbtest-ran uv run --frozen python - "$CI_LOG_DIR/dbtest.xml" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
cases = list(root.iter("testcase"))
skipped = [
    case.get("name")
    for case in cases
    for skip in case.findall("skipped")
    if skip.get("type") == "pytest.skip"
]
print(f"dbtest: {len(cases)} collected, {len(skipped)} skipped")
if len(cases) < 100 or skipped:
    raise SystemExit("Database tier did not fully run; inspect dbtest.log and dbtest.xml")
PY
fi

if [[ "$CI_TIER" == all || "$CI_TIER" == frontend ]]; then
  cd "$CI_REPO/apps/frontend"
  npm ci --no-audit --no-fund
  run_check eslint npm run lint
  run_check prettier npm run format:check
  run_check typescript npm run type-check
  run_check vitest npm run test
  # Both guards write .next, so run them sequentially.
  run_check server-only node scripts/check-server-only-guard.mjs
  run_check runtime-config node scripts/check-runtime-site-config.mjs
fi

printf '\n'
printf '%s\n' "${CI_RESULTS[@]}" | tee "$CI_LOG_DIR/summary.txt"
printf '\nLogs: %s\n' "$CI_LOG_DIR"
exit "$CI_FAILED"
