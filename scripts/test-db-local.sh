#!/usr/bin/env bash
# Run the database tests against one long-lived local PostgreSQL.
#
#   scripts/test-db-local.sh run [pytest args...]  fresh databases, then the dbtest tier
#   scripts/test-db-local.sh env                   fresh databases, then print the TEST_DB_* exports
#   scripts/test-db-local.sh reset                 drop this checkout's databases
#   scripts/test-db-local.sh clean                 drop databases of checkouts that no longer exist
#   scripts/test-db-local.sh up | down
#
# One container per machine, shared by every checkout and worktree. Its data
# lives in memory and nothing is fsynced, because a test database is
# disposable. Each checkout gets its own databases, named from its path, so
# worktrees on different branches (and different migration heads) never share
# tables or an alembic_version row.
#
# Every run starts from empty databases. The fixtures migrate a worker's
# database on first use, and some tests write settings into the base database
# itself (tests/integration/storage/test_identity_namespace.py), so a reused
# database would carry one run's leftovers into the next. On tmpfs, creating
# them again costs well under a second.
#
# Two runs on the same databases (one worktree, two agents) take turns rather
# than dropping each other's tables.
#
# Settings (environment):
#   TEST_DB_LOCAL_CONTAINER  container name   (cloud-agent-test-postgres)
#   TEST_DB_LOCAL_PORT       host port        (54339, bound to 127.0.0.1)
#   TEST_DB_LOCAL_IMAGE      image            (postgres:15, as CI)
#   TEST_DB_WORKERS          xdist workers    (min(nproc, 12))
#   TEST_DB_LOCAL_NAME       database name    (derived from the checkout path)
#
# TEST_DB_NAME is deliberately not an input: after `eval "$(... env)"` in one
# worktree it is still set in the shell that moves on to the next one.

set -euo pipefail

CONTAINER="${TEST_DB_LOCAL_CONTAINER:-cloud-agent-test-postgres}"
PORT="${TEST_DB_LOCAL_PORT:-54339}"
IMAGE="${TEST_DB_LOCAL_IMAGE:-postgres:15}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
UV_RUN="${UV_RUN:-uv run}"
# Marks a database this script created, and names the checkout that owns it.
OWNER_PREFIX="test-db-local owner: "

die() {
  echo "test-db-local: $*" >&2
  exit 1
}

usage() {
  sed -n '2,8p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

workers() {
  local count
  if [ -n "${TEST_DB_WORKERS:-}" ]; then
    count="${TEST_DB_WORKERS}"
  else
    count="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN)"
    # Measured on 20 CPUs: 142s with 4 workers, 94s with 8, 71s with 12 and
    # 76s with 16. Past twelve the run is bound by the shared server.
    count=$((count < 12 ? count : 12))
  fi
  [[ "${count}" =~ ^[1-9][0-9]*$ ]] || die "TEST_DB_WORKERS must be a positive whole number"
  echo "${count}"
}

database_name() {
  local name
  if [ -n "${TEST_DB_LOCAL_NAME:-}" ]; then
    name="${TEST_DB_LOCAL_NAME}"
  else
    name="ca_$(printf '%s' "${ROOT}" | sha1sum | cut -c1-10)_test_db"
  fi
  # The fixtures' own rule, checked here too because this script drops
  # databases by this name without going through them.
  if ! [[ "${name}" =~ ^[a-z0-9_]+$ && "${name}" == *_test_* ]]; then
    die "database name '${name}' must be lower-case letters, digits and underscores and contain _test_"
  fi
  # "<name>_gw<digits>" is a worker's database. A name of that form would put
  # this checkout's base database inside another checkout's set of workers.
  if [[ "${name}" =~ _gw[0-9]+$ ]]; then
    die "database name '${name}' must not end in _gw<digits>, which names a worker's database"
  fi
  echo "${name}"
}

export_env() {
  export TEST_DB_HOST=127.0.0.1
  export TEST_DB_PORT="${PORT}"
  export TEST_DB_USER=postgres
  export TEST_DB_PASSWORD=postgres
  TEST_DB_NAME="$(database_name)" || exit 1
  export TEST_DB_NAME
}

up() {
  command -v docker >/dev/null || die "docker is not installed"
  # Several agents may start at once: whoever loses the `docker run` race
  # retries until the winner's container shows up (it is not visible at once).
  local state attempt
  for attempt in $(seq 1 20); do
    state="$(docker container inspect --format '{{.State.Status}}' "${CONTAINER}" 2>/dev/null || true)"
    case "${state}" in
      running) break ;;
      "")
        docker run --detach --name "${CONTAINER}" \
          --restart unless-stopped \
          --publish "127.0.0.1:${PORT}:5432" \
          --env POSTGRES_PASSWORD=postgres \
          --tmpfs /var/lib/postgresql/data:rw,size=4g \
          --label org.hybridinference.cloud-agent.role=test-database \
          "${IMAGE}" \
          -c fsync=off -c synchronous_commit=off -c full_page_writes=off \
          -c max_connections=500 -c shared_buffers=512MB >/dev/null 2>&1 && break
        ;;
      created | restarting) ;;
      *) docker start "${CONTAINER}" >/dev/null 2>&1 && break ;;
    esac
    sleep 0.5
  done
  [ -n "$(docker container inspect --format '{{.Id}}' "${CONTAINER}" 2>/dev/null)" ] ||
    die "could not start ${CONTAINER} (is port ${PORT} free? set TEST_DB_LOCAL_PORT)"
  # Over TCP: during first start-up the image runs a temporary server on the
  # Unix socket only, which pg_isready would report as ready.
  local _
  for _ in $(seq 1 60); do
    if docker exec "${CONTAINER}" pg_isready -q -h 127.0.0.1 -U postgres 2>/dev/null; then
      return
    fi
    sleep 0.5
  done
  die "${CONTAINER} did not become ready; see: docker logs ${CONTAINER}"
}

psql_in() {
  docker exec -i "${CONTAINER}" psql -U postgres -d postgres -qAtX -v ON_ERROR_STOP=1 "$@"
}

# Hold a PostgreSQL advisory lock on this database until the script exits,
# or until the next call takes another one. The lock belongs to a psql session
# reading from this shell (fd 7), so it is released whenever that session ends
# (the shell going away, however it goes, or fd 7 being replaced): no lock file
# to go stale. FIFOs rather than `coproc`, which macOS's bash 3.2 lacks.
#
# With "try", return 1 at once when another run holds it instead of waiting.
lock_database() {
  local dir key got
  dir="$(mktemp -d)"
  mkfifo "${dir}/in" "${dir}/out"
  docker exec -i "${CONTAINER}" psql -U postgres -d postgres -qAtX <"${dir}/in" >"${dir}/out" &
  exec 7>"${dir}/in" 8<"${dir}/out"
  rm -rf "${dir}"
  key="hashtext('test-db-local:${TEST_DB_NAME}')"
  echo "SELECT pg_try_advisory_lock(${key});" >&7
  read -r got <&8 || die "could not reach ${CONTAINER} to lock ${TEST_DB_NAME}"
  if [ "${got}" != "t" ]; then
    [ "${1:-}" = try ] && return 1
    echo "test-db-local: another run is using ${TEST_DB_NAME}; waiting for it to finish" >&2
    echo "SELECT 'locked' FROM pg_advisory_lock(${key}) AS taken;" >&7
    read -r got <&8 || die "lost ${CONTAINER} while waiting for ${TEST_DB_NAME}"
  fi
}

# This checkout's databases: the name and "<name>_gw<digits>", matched
# literally, never as a pattern.
OWNED="datname = :'name' OR (starts_with(datname, :'name' || '_gw') \
  AND substr(datname, length(:'name') + 4) ~ '^[0-9]+$')"

# What marks a database as this checkout's.
mark() {
  printf '%s%s' "${OWNER_PREFIX}" "$1"
}

# Drop this checkout's databases. With a mark, only those still carrying it:
# `clean` passes the one it read before taking the lock, so a database a new
# checkout has taken over since is left alone.
drop_databases() {
  psql_in -v name="${TEST_DB_NAME}" -v mark="${1:-}" <<SQL
SELECT format('DROP DATABASE %I WITH (FORCE)', datname) FROM pg_database
WHERE (${OWNED})
  AND (:'mark' = '' OR shobj_description(oid, 'pg_database') = :'mark')
\gexec
SQL
}

# Refuse unless every database the drop would remove is this checkout's, or
# was left by one that no longer exists. Each worker's database is checked as
# well as the base one: it is dropped with them, and a mark on the base alone
# says nothing about it.
check_owner() {
  local rows name comment owner
  # Read in full first: a query that fails inside `done < <(...)` would end
  # the loop as if no database existed, and the drop would go ahead.
  rows="$(
    psql_in -F $'\t' -v name="${TEST_DB_NAME}" <<SQL
SELECT datname, coalesce(shobj_description(oid, 'pg_database'), '')
FROM pg_database WHERE ${OWNED} ORDER BY datname
SQL
  )" || die "could not read who owns ${TEST_DB_NAME}'s databases; dropping nothing"
  while IFS=$'\t' read -r name comment; do
    [ -n "${name}" ] || continue
    case "${comment}" in
      "${OWNER_PREFIX}"*)
        owner="${comment#"${OWNER_PREFIX}"}"
        if [ "${owner}" != "${ROOT}" ] && [ -d "${owner}" ]; then
          echo "test-db-local: ${name} belongs to ${owner}, which still exists" >&2
          exit 2
        fi
        ;;
      *)
        echo "test-db-local: ${name} exists and was not made by this script; refusing to drop it" >&2
        exit 2
        ;;
    esac
  done <<<"${rows}"
}

# Empty databases for this checkout: the base one, which some tests use
# directly, and one per xdist worker, as CI creates them.
fresh_databases() {
  local count index
  count="$(workers)"
  check_owner
  drop_databases
  local names=("${TEST_DB_NAME}") owner
  for ((index = 0; index < count; index++)); do
    names+=("${TEST_DB_NAME}_gw${index}")
  done
  owner="$(mark "${ROOT}")"
  for name in "${names[@]}"; do
    printf 'CREATE DATABASE %s;\n' "${name}"
    printf "COMMENT ON DATABASE %s IS '%s';\n" "${name}" "${owner//\'/\'\'}"
  done | psql_in >/dev/null
}

# The fixtures skip, rather than fail, when they cannot reach their database,
# so a green pytest alone does not prove the tier ran. CI and
# scripts/ci-local.sh apply the same check to the JUnit report.
check_tier_ran() {
  ${UV_RUN} python - "$1" <<'PY'
import sys
import xml.etree.ElementTree as ET

root = ET.parse(sys.argv[1]).getroot()
cases = list(root.iter("testcase"))
skipped = [
    f"{case.get('classname')}::{case.get('name')}"
    for case in cases
    for skip in case.findall("skipped")
    if skip.get("type") == "pytest.skip"
]
print(f"test-db-local: {len(cases)} collected, {len(skipped)} skipped")
if not cases:
    raise SystemExit("test-db-local: no database test ran")
if skipped:
    raise SystemExit(f"test-db-local: database tests skipped, so this run proved nothing: {skipped[:5]}")
PY
}

cmd="${1:-run}"
[ $# -gt 0 ] && shift
cd "${ROOT}"
# Match CI rather than the restrictive umask of an enclosing agent process, and
# use the repository's CLI pins rather than an enclosing agent's.
umask 022
unset CODEX_VERSION

case "${cmd}" in
  run)
    export_env
    up
    lock_database
    fresh_databases
    report="$(mktemp)"
    # The tier stays selected whatever is added (ARGS="-x", a path): without
    # it, extra arguments would also collect the live external tests. A later
    # -m of the caller's own replaces it.
    set -- -m "dbtest and not external" "$@"
    # Not exec: this shell keeps the lock's session open until pytest is done.
    status=0
    ${UV_RUN} pytest -n "$(workers)" --dist loadfile -q \
      --junitxml="${report}" "$@" || status=$?
    if [ "${status}" -eq 0 ]; then
      check_tier_ran "${report}" || status=$?
    fi
    rm -f "${report}"
    exit "${status}"
    ;;
  env)
    # Locked while creating the databases only: the caller's own pytest runs
    # after this exits, with at most TEST_DB_WORKERS workers.
    export_env
    up
    lock_database
    fresh_databases
    for name in TEST_DB_HOST TEST_DB_PORT TEST_DB_USER TEST_DB_PASSWORD TEST_DB_NAME; do
      printf 'export %s=%q\n' "${name}" "${!name}"
    done
    ;;
  up)
    up
    echo "${CONTAINER} ready on 127.0.0.1:${PORT}"
    ;;
  down)
    # The data is in memory: this discards every checkout's databases, and
    # fails any run in progress in any of them.
    docker rm --force "${CONTAINER}" >/dev/null 2>&1 || true
    echo "${CONTAINER} removed"
    ;;
  reset)
    export_env
    up
    lock_database
    check_owner
    drop_databases
    echo "dropped ${TEST_DB_NAME} and its worker copies"
    ;;
  clean)
    up
    # Only databases this script marked, and only when the checkout that
    # marked them is gone. Anything made another way is never touched. Each
    # set is dropped under the lock a run of it holds, and only the databases
    # that still carry the mark read here: a checkout can take a name over
    # between this scan and the drop.
    marked="$(
      psql_in -F $'\t' -v prefix="${OWNER_PREFIX}" <<'SQL'
SELECT DISTINCT regexp_replace(datname, '_gw[0-9]+$', ''),
       substr(shobj_description(oid, 'pg_database'), length(:'prefix') + 1)
FROM pg_database WHERE starts_with(shobj_description(oid, 'pg_database'), :'prefix')
ORDER BY 1
SQL
    )" || die "could not list the marked databases; dropping nothing"
    stale=()
    while IFS=$'\t' read -r name owner; do
      [ -n "${name}" ] || continue
      [ -d "${owner}" ] || stale+=("${name}"$'\t'"${owner}")
    done <<<"${marked}"
    for entry in ${stale[@]+"${stale[@]}"}; do
      TEST_DB_NAME="${entry%%$'\t'*}"
      owner="${entry#*$'\t'}"
      if ! lock_database try; then
        echo "kept ${TEST_DB_NAME} (a run is using it)"
        continue
      fi
      # Under the lock: a checkout restored at that path keeps its databases.
      if [ -d "${owner}" ]; then
        echo "kept ${TEST_DB_NAME} (its checkout ${owner} exists again)"
        continue
      fi
      drop_databases "$(mark "${owner}")"
      echo "dropped ${TEST_DB_NAME} (its checkout ${owner} is gone)"
    done
    ;;
  -h | --help | help)
    usage
    ;;
  *)
    usage >&2
    die "unknown command '${cmd}'"
    ;;
esac
