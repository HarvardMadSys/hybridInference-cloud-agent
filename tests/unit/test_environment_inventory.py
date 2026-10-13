"""Every environment variable the code reads is configurable and written down.

ENVIRONMENT.md ends with "the PR checklist enforces this; a new variable
without a row is a review stop". It did not: `AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS`
and `AGENT_CONTROL_PLANE_URL` were both added without rows, and the first was
worse than undocumented — it is the host allowlist that decides who receives
the pool's shared sandbox manager token, and it was never wired into the control-plane
compose file at all. The deployed stack could not turn it on. A checklist the
author ticks for themselves is not a control over the author.

Three invariants, and the first is the only cheap one:

* **Documented.** A variable read anywhere must appear in ENVIRONMENT.md.
  Cheap, and it is how an operator learns the variable exists.
* **Reachable.** A variable the *control plane* reads must be a key under
  `environment:` in `deploy/control-plane/docker-compose.yml`. Otherwise the
  code's fallback is the only behaviour the deployment can ever have — and for
  a security control, the fallback is "off" while the code, the tests and the
  review all say the control exists.
* **Baked in.** A `NEXT_PUBLIC_*` the web bundle reads must be an `ARG` in
  `Dockerfile.web` *and* a build argument of the `web` service. These are
  substituted at `next build`, so an unwired one is not a variable that
  defaults — it is a variable nothing can change, including setting it on the
  running container.
* **Runtime.** An `AGENT_WEB_*` the web *server* reads must be a key under
  `environment:` of the `web` service. The mirror image of the rule above:
  passed as a build argument instead, it is discarded before the server starts,
  and the deployment renders the neutral default with nothing saying why.

Only the control plane gets the compose check. Host variables are set by an
operator on the machine, and a check that demanded compose keys for those would
be demanding the wrong thing.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PLANE_COMPOSE = REPO_ROOT / "deploy" / "control-plane" / "docker-compose.yml"
WEB_DOCKERFILE = REPO_ROOT / "deploy" / "control-plane" / "Dockerfile.web"
INVENTORY = REPO_ROOT / "ENVIRONMENT.md"

#: The prefixes this project owns. A bare `PATH` or `HOME` read is not ours to
#: document, and listing every variable any dependency might read would make
#: the check noise that people learn to silence.
_VAR = re.compile(r'["\'](AGENT_[A-Z0-9_]+|GATEWAY_[A-Z0-9_]+|IDENTITY_[A-Z0-9_]+)["\']')

#: `process.env.NEXT_PUBLIC_FOO`. Property access rather than a quoted name,
#: which is how the bundler recognises them too — the substitution is textual,
#: so `process.env[name]` would not work in the app either.
_PUBLIC_VAR = re.compile(r"process\.env\.(NEXT_PUBLIC_[A-Z0-9_]+)")

#: `ARG NEXT_PUBLIC_FOO=`, declared in the builder stage.
_DOCKERFILE_ARG = re.compile(r"^ARG\s+(NEXT_PUBLIC_[A-Z0-9_]+)", re.MULTILINE)

#: `'AGENT_WEB_FOO'` — a quoted name in the web site-config modules. The server
#: reader takes the variable name as a string literal rather than reaching
#: through `process.env.FOO`, which is what makes it visible to this check.
_RUNTIME_WEB_VAR = re.compile(r'["\'](AGENT_WEB_[A-Z0-9_]+)["\']')

#: `next.config.js` reads them too, and it is not under `src/`.
_WEB_SOURCES = ("apps/frontend/src/**/*.ts", "apps/frontend/src/**/*.tsx", "apps/frontend/*.js")

#: Where the Python packages live. These patterns used to start at the
#: repository root, matched nothing once the packages moved under
#: `apps/backend/`, and every Python check here then passed without reading a
#: file. `_sources` now refuses a pattern that matches nothing.
_CONTROL_PLANE_SOURCES = "apps/backend/cloud_agent/**/*.py"
_PYTHON_SOURCES = (
    _CONTROL_PLANE_SOURCES,
    "apps/backend/cloud_agent_host/**/*.py",
    "apps/backend/cloud_agent_shared/**/*.py",
    "migrations/**/*.py",
)

#: Control-plane modules that write a *host's* environment: compute setup and
#: the host bootstrap render, require and forbid the names in a new machine's
#: `.env`. Those names are host variables, which the control plane's compose
#: file has no business setting, so in these two modules only an actual read of
#: the control plane's own environment counts. The names still need rows in
#: ENVIRONMENT.md, which the inventory check below enforces.
_HOST_ENV_WRITERS = (
    "apps/backend/cloud_agent/application/compute_setup.py",
    "apps/backend/cloud_agent/providers/compute/host_bootstrap.py",
)

#: An actual read: `os.environ.get(...)`, `os.environ[...]`, `getenv(...)` or
#: `setting_env(...)` with the name as its literal argument.
_ENV_READ = re.compile(
    r"(?:environ\.get\(|environ\[|getenv\(|setting_env\()\s*"
    r'["\'](AGENT_[A-Z0-9_]+|GATEWAY_[A-Z0-9_]+|IDENTITY_[A-Z0-9_]+)["\']'
)

#: Quoted like a variable, and not one: Python names with a prefix this project
#: owns that the code mentions as strings. Each entry needs a reason, for the
#: same reason the exemptions below do.
_NOT_VARIABLES = {
    # A message constant that `owner_access.py` exports by name in `__all__`.
    "AGENT_ACCESS_WITHDRAWN",
}

#: A row whose first cell is `AGENT_TUNNEL_*` documents a family: the code
#: builds the names from the prefix, and the row lists the suffixes. Only a
#: table row's first cell counts, so prose saying a family does *not* exist
#: and a struck-through removed row document nothing.
_FAMILY_ROW = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]*)_\*`\s*\|", re.MULTILINE)

#: Read by the control plane, and correctly absent from its compose file.
#: Each entry needs a reason, because "add it to the exemption list" is the
#: obvious way to make this test stop failing without fixing anything.
_COMPOSE_EXEMPT = {
    # Deliberately not offered by this compose file, which mounts the key as a
    # read-only file instead. The code still accepts an inline PEM for
    # deployments that do not use this stack; wiring it here would put a
    # private key in the container environment, where `docker inspect`, crash
    # dumps and most log shippers can reach it. ENVIRONMENT.md says so in the
    # row, so an operator does not set it and wonder why nothing happens.
    "AGENT_GITHUB_APP_PRIVATE_KEY",
}


def _sources(*globs: str) -> list[Path]:
    found: list[Path] = []
    for pattern in globs:
        # Test files only: a bare "test" in the name also skipped
        # `storage_attestation.py`.
        matched = [
            p
            for p in REPO_ROOT.glob(pattern)
            if not (p.name.startswith("test_") or ".test." in p.name)
        ]
        # A pattern that matches nothing makes every check below a pass over
        # an empty set, which is how the package move went unnoticed.
        assert matched, f"{pattern} matches no source file; update the pattern"
        found += matched
    return found


def _vars_read(paths: list[Path], pattern: re.Pattern[str] = _VAR) -> set[str]:
    names: set[str] = set()
    for path in paths:
        names |= set(pattern.findall(path.read_text()))
    return names


def _compose_environment_keys() -> set[str]:
    """Keys under `environment:` — not merely text present in the file.

    A variable mentioned only in a comment is not wired to anything, and the
    comments here are long enough that a substring search over the raw file
    would pass on the strength of the sentence explaining the variable.
    """
    document = yaml.safe_load(CONTROL_PLANE_COMPOSE.read_text())
    keys: set[str] = set()
    for service in (document.get("services") or {}).values():
        environment = service.get("environment") or {}
        keys |= set(environment if isinstance(environment, dict) else ())
    return keys


def test_control_plane_variables_are_wired_into_compose() -> None:
    """A variable the control plane reads can be set on the deployed stack."""
    sources = _sources(_CONTROL_PLANE_SOURCES)
    writers = [REPO_ROOT / path for path in _HOST_ENV_WRITERS]
    moved = [str(path) for path in writers if path not in sources]
    assert not moved, f"{moved} no longer exist; update _HOST_ENV_WRITERS"
    read = (
        _vars_read([path for path in sources if path not in writers])
        | _vars_read(writers, _ENV_READ)
    ) - _NOT_VARIABLES
    assert read, "no variable reads found under apps/backend/cloud_agent/ — regex drift?"
    read -= _COMPOSE_EXEMPT
    unreachable = sorted(read - _compose_environment_keys())
    assert not unreachable, (
        "the control plane reads these, but they are not keys under "
        f"`environment:` in {CONTROL_PLANE_COMPOSE.relative_to(REPO_ROOT)}, so a "
        f"deployment cannot set them: {unreachable}"
    )


def _web_build_args() -> set[str]:
    """Build arguments of the `web` service — the only way in for these.

    Under `build.args`, not `environment:`: the two look alike in the file and
    mean opposite things here. A `NEXT_PUBLIC_*` under `environment:` would be
    set on a container that has already been built with the value missing.
    """
    document = yaml.safe_load(CONTROL_PLANE_COMPOSE.read_text())
    build = ((document.get("services") or {}).get("web") or {}).get("build") or {}
    args = build.get("args") or {}
    return set(args if isinstance(args, dict) else ())


def _web_runtime_env_keys() -> set[str]:
    """Keys under `environment:` of the `web` service — how a runtime read is set.

    Not `build.args`: a value substituted at `next build` is not available to
    the server that renders the request, which is the whole distinction this
    check exists to keep.
    """
    document = yaml.safe_load(CONTROL_PLANE_COMPOSE.read_text())
    environment = ((document.get("services") or {}).get("web") or {}).get("environment") or {}
    return set(environment if isinstance(environment, dict) else ())


def test_web_build_arguments_are_wired() -> None:
    """A `NEXT_PUBLIC_*` the bundle reads can be set by the deployment.

    `NEXT_PUBLIC_STATUS_URL` had a row in ENVIRONMENT.md and a reader in the
    header, and appeared in no Dockerfile and no compose file. It was not a
    knob left at its default — it was a knob wired to nothing, and the header
    rendered `<a href="">Status</a>` on every deployment: an empty href
    resolves to the current document, so the link reopened the page you were
    already on.
    """
    read = _vars_read(_sources(*_WEB_SOURCES), _PUBLIC_VAR)
    assert read, "no NEXT_PUBLIC_* reads found under apps/frontend/ — regex drift?"

    undeclared = sorted(read - set(_DOCKERFILE_ARG.findall(WEB_DOCKERFILE.read_text())))
    assert not undeclared, (
        "the web bundle reads these, but they are not declared `ARG` in "
        f"{WEB_DOCKERFILE.relative_to(REPO_ROOT)}, so `docker build` drops them: {undeclared}"
    )

    unpassed = sorted(read - _web_build_args())
    assert not unpassed, (
        "the web bundle reads these, but the `web` service in "
        f"{CONTROL_PLANE_COMPOSE.relative_to(REPO_ROOT)} passes no `build.args` entry for "
        f"them, so this stack builds an image that can never carry them: {unpassed}"
    )


def test_web_runtime_variables_are_wired() -> None:
    """An `AGENT_WEB_*` the web server reads can be set on the running container.

    These are the deployment's identity — name, organisation, status page, the
    gateway's origin — and they are read per request, so an operator changes one
    by restarting the container rather than rebuilding the image. That only
    works if the variable reaches the container: a runtime read wired as a
    build argument is dropped before the server starts, and the page renders
    the neutral default with nothing to say the value was ignored.
    """
    read = _vars_read(
        _sources("apps/frontend/src/config/site.ts", "apps/frontend/src/config/site.server.ts"),
        _RUNTIME_WEB_VAR,
    )
    assert read, "no AGENT_WEB_* reads found under apps/frontend/src/config/ — regex drift?"

    missing = sorted(read - _web_runtime_env_keys())
    assert not missing, (
        "the web server reads these per request, but they are not keys under "
        f"`environment:` of the `web` service in "
        f"{CONTROL_PLANE_COMPOSE.relative_to(REPO_ROOT)}, so the deployment cannot set "
        f"them without a rebuild: {missing}"
    )


def test_every_variable_read_has_an_inventory_row() -> None:
    """A variable an operator might need to set is written down."""
    read = (
        _vars_read(_sources(*_PYTHON_SOURCES)) | _vars_read(_sources(*_WEB_SOURCES), _PUBLIC_VAR)
    ) - _NOT_VARIABLES
    assert read, "no variable reads found — regex drift?"
    inventory = INVENTORY.read_text()
    families = set(_FAMILY_ROW.findall(inventory))
    # Backticked, which is how every row writes them — and how a grouped row
    # like ``AGENT_SANDBOX_UID`` / ``AGENT_SANDBOX_GID`` still matches both. A
    # family row covers its prefix and every name built from it.
    undocumented = sorted(
        name
        for name in read
        if f"`{name}`" not in inventory
        and not any(name == family or name.startswith(f"{family}_") for family in families)
    )
    assert not undocumented, (
        f"add a row to {INVENTORY.name} for each of these, in the right section, "
        f"marked NEW: {undocumented}"
    )


def test_only_a_family_row_documents_a_prefix() -> None:
    """Prose about a family, or a removed one, covers no name built from it."""
    inventory = (
        "| `AGENT_TUNNEL_*` | existing | Inference SSH tunnel. |\n"
        "There is no `AGENT_SANDBOX_ARCHIVE_*` environment surface.\n"
        "| ~~`AGENT_EGRESS_PROXY_*`~~ | **removed** | Nothing reads them. |\n"
    )
    assert _FAMILY_ROW.findall(inventory) == ["AGENT_TUNNEL"]


def test_a_host_env_writer_still_counts_its_own_reads() -> None:
    """A writer's host names are not reads; its own environment reads still are."""
    source = (
        'host_env = {"AGENT_HOST_SLOTS": "3"}\n'
        'sha = os.environ.get("AGENT_DEPLOYMENT_SHA", "")\n'
        'ssh = os.environ["AGENT_CONTROL_PLANE_SSH_HOST"]\n'
        'url = setting_env("AGENT_COMPUTE_REGISTRATION_URL")\n'
    )
    assert set(_ENV_READ.findall(source)) == {
        "AGENT_DEPLOYMENT_SHA",
        "AGENT_CONTROL_PLANE_SSH_HOST",
        "AGENT_COMPUTE_REGISTRATION_URL",
    }
