"""Every environment variable is classified, and settings are read through their resolver.

``tests/support/env_inventory.py`` says where each variable belongs: an admin
setting, a heartbeat-delivered host setting, deployment configuration, and so
on. These tests hold the code to it:

* **Classified.** A variable that Python reads, that a deploy or tooling shell
  script takes from its environment (``tests/support/env_shell.py``), or that a
  Compose file, an example env file, a Dockerfile ``ARG`` or the web app names,
  has exactly one entry; an entry nothing names any more is removed. A shell
  read whose variable the script computes is listed with its reason.
* **Consistent.** The ``admin`` entries are exactly the environment names of
  ``SETTINGS``, the ``host_admin`` ones those of ``HOST_SETTINGS`` (plus a
  host's slots), and the ``retired`` ones the former names still mapped.
* **No bypass.** Backend code reads a registered setting through its resolver
  (``setting_env``, ``settings_environment``, ``get_setting``). A raw read of
  one is named in ``RAW_SETTING_READS`` with its reason; a host setting is read
  from a host's environment only where that is its bootstrap value; a read
  whose variable the source does not name is a registry loop listed in
  ``REGISTRY_READS``; and backend code that hands its whole environment to a
  child says where.
* **Imported.** A new registration with an environment name joins
  ``MIGRATED_SETTINGS``, the only list an existing database imports from.

The scan (``tests/support/env_scan.py``) follows the spellings this repository
uses; what it cannot see is listed in its docstring and in the P1 spec. The
checks are pure functions of the scan, so the tests at the end feed each one a
regression and expect it to fail.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import env_inventory as inventory
from tests.support.env_scan import (
    EnvRead,
    TemplateName,
    find_env_reads,
    python_sources,
    scan_paths,
    template_names,
)
from tests.support.env_shell import ShellDynamic, ShellInput, scan_shell, shell_sources

from cloud_agent.settings import MIGRATED_SETTINGS, SETTINGS
from cloud_agent_shared.host_settings import HOST_SETTINGS
from cloud_agent_shared.sandbox_manager_compat import LEGACY_ENV_NAMES

if TYPE_CHECKING:
    from collections.abc import Iterable

REPO = Path(__file__).resolve().parents[2]
BACKEND = "apps/backend/"
#: Where a variable an operator sets for a production stack is named.
PRODUCTION_TEMPLATES = ("deploy/control-plane/", "deploy/host/")
#: The one ``host_admin`` variable that is not in HOST_SETTINGS.
HOST_ADMIN_OUTSIDE_REGISTRY = frozenset({"AGENT_HOST_SLOTS"})
#: Retired names that are not former Sandbox Manager spellings.
RETIRED_OUTSIDE_LEGACY = frozenset({"AGENT_RUNNER_REPLICAS"})

SETTING_ENVS = frozenset(setting.env for setting in SETTINGS if setting.env)
HOST_SETTING_ENVS = frozenset(setting.env for setting in HOST_SETTINGS)
REGISTRIES = {"HOST_SETTINGS": HOST_SETTING_ENVS}


@dataclass(frozen=True)
class Scan:
    reads: list[EnvRead]
    templates: list[TemplateName]
    shell: list[ShellInput] = field(default_factory=list)
    shell_dynamic: list[ShellDynamic] = field(default_factory=list)
    #: Every function the scanned shell scripts define.
    shell_functions: frozenset[str] = frozenset()


def scan_scripts(scripts: dict[str, str]) -> tuple[list[ShellInput], list[ShellDynamic]]:
    """Shell inputs and computed reads of ``scripts`` (path -> text)."""
    inputs: list[ShellInput] = []
    dynamics: list[ShellDynamic] = []
    for path, text in sorted(scripts.items()):
        found, computed = scan_shell(text, path, inventory.SHELL_HELPERS)
        inputs += found
        dynamics += computed
    return inputs, dynamics


def _files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO,
        capture_output=True,
        check=True,
    ).stdout
    return sorted(
        {name.decode() for name in listed.split(b"\0") if name and (REPO / name.decode()).is_file()}
    )


@pytest.fixture(scope="module")
def scan() -> Scan:
    files = _files()
    sources = python_sources(REPO, files)
    # A pattern that matches nothing makes every check here a pass over an
    # empty set: the failure that let test_environment_inventory read nothing
    # after the packages moved.
    assert any(path.as_posix().endswith("cloud_agent/settings.py") for path in sources)
    reads = scan_paths(sources, root=REPO, package_root=REPO / "apps" / "backend")
    templates = template_names(REPO, files)
    assert {t.kind for t in templates} >= {"compose", "container", "example", "web"}
    scripts = {path: (REPO / path).read_text(encoding="utf-8") for path in shell_sources(files)}
    assert "deploy/host/sandbox_host.sh" in scripts
    shell, dynamic = scan_scripts(scripts)
    functions = frozenset(
        name
        for text in scripts.values()
        for name in re.findall(r"^[ \t]*(?:function[ \t]+)?(\w+)[ \t]*\(\)", text, re.MULTILINE)
    )
    return Scan(reads, templates, shell, dynamic, functions)


# -- the checks ------------------------------------------------------------


def _covering(name: str, families: Iterable[inventory.Family]) -> list[inventory.Family]:
    """The families that classify ``name``; a prefix read ``X_*`` needs prefix ``X_``."""
    if name.endswith("*"):
        return [family for family in families if name[:-1] in family.prefixes]
    return [
        family
        for family in families
        if name in family.names or any(name.startswith(prefix) for prefix in family.prefixes)
    ]


def _named(scan: Scan) -> dict[str, list[str]]:
    """Every variable named anywhere, with where."""
    found: dict[str, list[str]] = {}
    for read in scan.reads:
        names: Iterable[str] = read.names or ()
        registry = inventory.REGISTRY_READS.get((read.module, read.function))
        if read.names is None and registry is not None:
            names = REGISTRIES[registry]
        for name in names:
            found.setdefault(name, []).append(f"{read.module}:{read.line} ({read.via})")
    for template in scan.templates:
        found.setdefault(template.name, []).append(f"{template.path} ({template.kind})")
    for read in scan.shell:
        if (read.path, read.name) not in inventory.SHELL_NOT_INPUTS:
            found.setdefault(read.name, []).append(f"{read.path}:{read.line} (shell {read.how})")
    return found


def unclassified(
    scan: Scan, families: Iterable[inventory.Family] = inventory.FAMILIES
) -> list[str]:
    families = tuple(families)
    return [
        f"{name}: {', '.join(sorted(set(where))[:3])}"
        for name, where in sorted(_named(scan).items())
        if not _covering(name, families)
    ]


def table_problems(families: Iterable[inventory.Family] = inventory.FAMILIES) -> list[str]:
    families = tuple(families)
    problems = []
    seen: dict[str, int] = {}
    for family in families:
        if not family.reason.strip():
            problems.append(f"a {family.disposition} family has no reason")
        if not family.names and not family.prefixes:
            problems.append(f"a {family.disposition} family names nothing")
        for name in family.names:
            seen[name] = seen.get(name, 0) + 1
        problems += [f"prefix {p!r} does not end with _" for p in family.prefixes if p[-1:] != "_"]
    problems += [f"{name} is classified {count} times" for name, count in seen.items() if count > 1]
    prefixes = [prefix for family in families for prefix in family.prefixes]
    for prefix in prefixes:
        problems += [
            f"{name} is also covered by prefix {prefix}" for name in seen if name.startswith(prefix)
        ]
        problems += [
            f"prefix {other} is inside prefix {prefix}"
            for other in prefixes
            if other != prefix and other.startswith(prefix)
        ]
    return problems


def stale_entries(
    scan: Scan, families: Iterable[inventory.Family] = inventory.FAMILIES
) -> list[str]:
    named = set(_named(scan)) | SETTING_ENVS | HOST_SETTING_ENVS | set(LEGACY_ENV_NAMES.values())
    problems = []
    for family in families:
        problems += [f"{name} is named nowhere now" for name in family.names if name not in named]
        problems += [
            f"prefix {prefix} matches nothing now"
            for prefix in family.prefixes
            if not any(name.startswith(prefix) or name == f"{prefix}*" for name in named)
        ]
    return problems


def registry_mismatches(families: Iterable[inventory.Family] = inventory.FAMILIES) -> list[str]:
    by: dict[str, set[str]] = {}
    for family in families:
        by.setdefault(family.disposition, set()).update(family.names)
    problems = []
    for label, expected, actual in (
        ("admin", SETTING_ENVS, by.get("admin", set())),
        (
            "host_admin",
            HOST_SETTING_ENVS | HOST_ADMIN_OUTSIDE_REGISTRY,
            by.get("host_admin", set()),
        ),
        (
            "retired",
            set(LEGACY_ENV_NAMES.values()) | RETIRED_OUTSIDE_LEGACY,
            by.get("retired", set()),
        ),
    ):
        problems += [
            f"{name} is registered but not classified {label}" for name in expected - actual
        ]
        problems += [
            f"{name} is classified {label} but not registered" for name in actual - expected
        ]
    return problems


def resolver_bypasses(reads: Iterable[EnvRead]) -> list[str]:
    """Backend reads that skip a registered setting's resolver."""
    problems = []
    for read in reads:
        if not read.module.startswith(BACKEND) or read.module in inventory.RESOLVER_MODULES:
            continue
        names = set(read.names or ())
        if read.names is None and (read.module, read.function) in inventory.REGISTRY_READS:
            names = set(REGISTRIES[inventory.REGISTRY_READS[read.module, read.function]])
        where = f"{read.module}:{read.line} {read.function}"
        for name in sorted(names & SETTING_ENVS):
            if read.via in ("raw", "compat") and (
                (read.module, read.function, name) not in inventory.RAW_SETTING_READS
            ):
                problems.append(
                    f"{where} reads the admin setting {name} from the environment: read it "
                    "with setting_env/get_setting, or add the read to RAW_SETTING_READS "
                    "with the reason the saved value cannot be used"
                )
        for name in sorted(names & HOST_SETTING_ENVS):
            if not read.module.startswith(f"{BACKEND}cloud_agent_host/"):
                problems.append(
                    f"{where} reads the host setting {name}: hosts apply it from the "
                    "heartbeat; the control plane reads HOST_SETTINGS values from the database"
                )
            elif (read.module, read.function) not in inventory.HOST_BOOTSTRAP_READS:
                problems.append(
                    f"{where} reads the host setting {name} from the host's environment "
                    "outside HOST_BOOTSTRAP_READS: use the heartbeat value"
                )
    return problems


def unexplained_reads(reads: Iterable[EnvRead]) -> list[str]:
    """Reads whose variable the source does not name, and whole-environment copies."""
    problems = []
    for read in reads:
        if not read.module.startswith(BACKEND) or read.module in inventory.RESOLVER_MODULES:
            continue
        key = (read.module, read.function)
        if read.via == "copy":
            if not read.embedded and key not in inventory.ENVIRONMENT_COPIES:
                problems.append(
                    f"{read} hands the whole environment on: name the variables, or add "
                    "the place to ENVIRONMENT_COPIES with the reason"
                )
        elif (
            read.names is None
            and read.via != "helper"
            and (key not in inventory.REGISTRY_READS or not read.argument.endswith(".env"))
        ):
            problems.append(f"{read}: name the variable, or read it through a resolver")
    return problems


def unmigrated_registrations(settings: Iterable[object], migrated: Iterable[object]) -> list[str]:
    migrated_keys = {setting.key for setting in migrated}  # type: ignore[attr-defined]
    outside = {
        setting.env  # type: ignore[attr-defined]
        for setting in settings
        if setting.env and setting.key not in migrated_keys  # type: ignore[attr-defined]
    }
    return [
        f"{name} is a new environment import outside MIGRATED_SETTINGS: an existing "
        "database would never read it. Register it in MIGRATED_SETTINGS"
        for name in sorted(outside - inventory.UNMIGRATED_ENV_SETTINGS)
    ] + [
        f"{name} left UNMIGRATED_ENV_SETTINGS; remove it from the frozen list"
        for name in sorted(inventory.UNMIGRATED_ENV_SETTINGS - outside)
    ]


def unused_exceptions(reads: list[EnvRead]) -> list[str]:
    problems = []
    for module in inventory.RESOLVER_MODULES:
        if not any(read.module == module for read in reads):
            problems.append(f"{module} reads nothing now")
    for module, function, name in inventory.RAW_SETTING_READS:
        if name not in SETTING_ENVS:
            problems.append(f"{name} in RAW_SETTING_READS is not an admin setting")
        if not any(
            (read.module, read.function) == (module, function) and name in (read.names or ())
            for read in reads
        ):
            problems.append(f"{module} {function} no longer reads {name}")
    for label, table in (
        ("HOST_BOOTSTRAP_READS", inventory.HOST_BOOTSTRAP_READS),
        ("REGISTRY_READS", inventory.REGISTRY_READS),
        ("ENVIRONMENT_COPIES", inventory.ENVIRONMENT_COPIES),
    ):
        for module, function in table:
            used = [read for read in reads if (read.module, read.function) == (module, function)]
            if label == "HOST_BOOTSTRAP_READS":
                used = [
                    read
                    for read in used
                    if set(read.names or ()) & HOST_SETTING_ENVS
                    or (read.names is None and (module, function) in inventory.REGISTRY_READS)
                ]
            elif label == "REGISTRY_READS":
                used = [read for read in used if read.names is None]
            else:
                used = [read for read in used if read.via == "copy"]
            if not used:
                problems.append(f"{label} entry {module} {function} matches no read now")
    return problems


def shell_problems(scan: Scan) -> list[str]:
    """Computed shell reads without a reason, and shell exceptions nothing needs."""
    problems = [
        f"{read.path}:{read.line} {read.function} reads a variable it computes "
        f"({read.construct}): read a literal name, or add it to SHELL_DYNAMIC_READS"
        for read in scan.shell_dynamic
        if (read.path, read.function, read.construct) not in inventory.SHELL_DYNAMIC_READS
    ]
    computed = {(read.path, read.function, read.construct) for read in scan.shell_dynamic}
    problems += [
        f"SHELL_DYNAMIC_READS entry {key} matches no read now"
        for key in inventory.SHELL_DYNAMIC_READS
        if key not in computed
    ]
    inputs = {(read.path, read.name) for read in scan.shell}
    problems += [
        f"SHELL_NOT_INPUTS entry {key} matches no read now"
        for key in inventory.SHELL_NOT_INPUTS
        if key not in inputs
    ]
    problems += [
        f"SHELL_HELPERS names {name}, which no script defines now"
        for name in inventory.SHELL_HELPERS
        if name not in scan.shell_functions
    ]
    return problems


def misplaced(scan: Scan, families: Iterable[inventory.Family] = inventory.FAMILIES) -> list[str]:
    """Internal names an operator template offers, and tooling names production reads."""
    families = tuple(families)

    def disposition(name: str) -> str | None:
        covering = _covering(name, families)
        return covering[0].disposition if covering else None

    problems = []
    for template in scan.templates:
        kind = disposition(template.name)
        if kind == "internal" and template.kind in ("example", "compose"):
            problems.append(
                f"{template.path} lets an operator set {template.name}, which this code sets"
            )
        if kind == "tooling" and template.path.startswith(PRODUCTION_TEMPLATES):
            problems.append(f"{template.path} names the tooling variable {template.name}")
    for read in scan.reads:
        for name in read.names or ():
            if read.module.startswith(BACKEND) and disposition(name) == "tooling":
                problems.append(f"{read.module}:{read.line} reads the tooling variable {name}")
    return problems


# -- the repository --------------------------------------------------------


def test_every_variable_is_classified(scan: Scan) -> None:
    problems = unclassified(scan)
    assert problems == [], (
        "add each to a family in tests/support/env_inventory.py, with where its value "
        "should come from:\n" + "\n".join(problems)
    )


def test_the_table_is_well_formed() -> None:
    assert table_problems() == []


def test_every_entry_is_still_named(scan: Scan) -> None:
    assert stale_entries(scan) == []


def test_registries_match_the_table() -> None:
    assert registry_mismatches() == []


def test_no_backend_code_reads_a_setting_past_its_resolver(scan: Scan) -> None:
    problems = resolver_bypasses(scan.reads)
    assert problems == [], "\n".join(problems)


def test_every_read_names_its_variable(scan: Scan) -> None:
    problems = unexplained_reads(scan.reads)
    assert problems == [], "\n".join(problems)


def test_new_environment_imports_are_migrated() -> None:
    assert unmigrated_registrations(SETTINGS, MIGRATED_SETTINGS) == []


def test_every_exception_is_still_needed(scan: Scan) -> None:
    assert unused_exceptions(scan.reads) == []


def test_every_shell_read_names_its_variable(scan: Scan) -> None:
    problems = shell_problems(scan)
    assert problems == [], "\n".join(problems)


def test_internal_and_tooling_names_stay_in_their_place(scan: Scan) -> None:
    assert misplaced(scan) == []


# -- each check fails on a regression --------------------------------------


def _reads(source: str, module: str = "apps/backend/cloud_agent/new.py") -> list[EnvRead]:
    return find_env_reads(source, module)


def test_a_new_variable_must_be_classified() -> None:
    reads = _reads('import os\nos.environ.get("AGENT_NEW_KNOB")\n')
    template = TemplateName("deploy/host/.env.example", "example", "AGENT_OTHER_KNOB")
    problems = unclassified(Scan(reads, [template]))
    assert [problem.split(":")[0] for problem in problems] == ["AGENT_NEW_KNOB", "AGENT_OTHER_KNOB"]
    assert unclassified(Scan(_reads('import os\nos.environ.get("AGENT_DATABASE_URL")\n'), [])) == []


def test_a_mapping_read_must_be_classified() -> None:
    """The spelling ``egress.network_from_env`` uses, which a call-only scan misses."""
    source = (
        "import os\n"
        "def network_from_env(env=None):\n"
        "    source = env if env is not None else dict(os.environ)\n"
        '    return source.get("AGENT_EGRESS_EXTRA_RELAY")\n'
    )
    assert [p.split(":")[0] for p in unclassified(Scan(_reads(source), []))] == [
        "AGENT_EGRESS_EXTRA_RELAY"
    ]


def test_a_duplicate_or_overlapping_entry_fails() -> None:
    first = inventory.Family("deployment", "one", names=("AGENT_X", "AGENT_TUNNEL_Y"))
    second = inventory.Family("tooling", "two", names=("AGENT_X",), prefixes=("AGENT_",))
    problems = table_problems([first, second, inventory.Family("tooling", " ", names=("Z",))])
    assert "AGENT_X is classified 2 times" in problems
    assert "AGENT_TUNNEL_Y is also covered by prefix AGENT_" in problems
    assert "a tooling family has no reason" in problems
    assert table_problems([*inventory.FAMILIES, first]) != []


def test_an_entry_nothing_names_fails(scan: Scan) -> None:
    gone = inventory.Family("deployment", "removed reader", names=("AGENT_NO_LONGER_READ",))
    assert stale_entries(scan, [gone]) == ["AGENT_NO_LONGER_READ is named nowhere now"]


def test_registering_a_deployment_variable_fails() -> None:
    """Registered names are exactly the admin entries, so registration is a reviewed move."""
    moved = [
        inventory.Family(
            f.disposition,
            f.reason,
            tuple(n for n in f.names if n != "AGENT_DEFAULT_MODEL"),
            f.prefixes,
        )
        for f in inventory.FAMILIES
    ]
    moved.append(inventory.Family("deployment", "x", names=("AGENT_DEFAULT_MODEL",)))
    assert registry_mismatches(moved) == [
        "AGENT_DEFAULT_MODEL is registered but not classified admin"
    ]


def test_a_raw_read_of_a_setting_fails_outside_its_exceptions() -> None:
    source = (
        "import os\n"
        "from cloud_agent.settings import setting_env\n"
        "from cloud_agent_shared.sandbox_manager_compat import manager_env\n"
        "def f():\n"
        '    os.environ.get("AGENT_REPO_ALLOWLIST")\n'
        '    manager_env("AGENT_SANDBOX_MANAGER_TOKEN")\n'
        '    setting_env("AGENT_REPO_ALLOWLIST")\n'
        '    os.environ.get("AGENT_DATABASE_URL")\n'
    )
    problems = resolver_bypasses(_reads(source))
    assert len(problems) == 2
    assert "AGENT_REPO_ALLOWLIST" in problems[0] and "AGENT_SANDBOX_MANAGER_TOKEN" in problems[1]
    # The same read where it is a documented exception.
    pgadmin = (
        'import os\ndef deployment_pgadmin_services():\n    os.environ.get("AGENT_PGADMIN_URL")\n'
    )
    assert (
        resolver_bypasses(_reads(pgadmin, "apps/backend/cloud_agent/api/admin_database.py")) == []
    )
    assert resolver_bypasses(_reads(pgadmin, "apps/backend/cloud_agent/api/other.py")) != []
    # Scripts outside the backend are not held to the resolver.
    assert resolver_bypasses(_reads(source, "deploy/preflight/check.py")) == []


def test_a_host_setting_read_outside_its_bootstrap_fails() -> None:
    host = 'import os\ndef new_reader():\n    os.environ.get("AGENT_TERMINAL_MAX_SESSIONS")\n'
    assert resolver_bypasses(_reads(host, "apps/backend/cloud_agent_host/runner.py")) != []
    control = (
        "from cloud_agent.settings import setting_env\n"
        'def f():\n    setting_env("AGENT_TIMEOUT_S")\n'
    )
    assert "control plane reads HOST_SETTINGS" in resolver_bypasses(_reads(control))[0]
    bootstrap = (
        'import os\ndef _chat_idle_timeout_s():\n    os.environ.get("AGENT_CHAT_IDLE_TIMEOUT_S")\n'
    )
    assert resolver_bypasses(_reads(bootstrap, "apps/backend/cloud_agent_host/runner.py")) == []


def test_an_unnamed_read_or_copy_fails_outside_its_exceptions() -> None:
    loop = (
        "import os\n"
        "def create_app():\n"
        "    for definition in HOST_SETTINGS:\n"
        "        os.environ.get(definition.env)\n"
    )
    module = "apps/backend/cloud_agent_host/sandbox_manager.py"
    assert unexplained_reads(_reads(loop, module)) == []
    assert unexplained_reads(_reads(loop, "apps/backend/cloud_agent_host/other.py")) != []
    dynamic = "import os\ndef f(name):\n    return os.getenv(name)\nf(compute())\n"
    assert unexplained_reads(_reads(dynamic)) != []
    copy = "import os, subprocess\ndef f():\n    subprocess.run(['x'], env=dict(os.environ))\n"
    assert unexplained_reads(_reads(copy)) != []
    assert unexplained_reads(_reads(copy, "apps/backend/cloud_agent_host/docker_engine.py")) != []


@dataclass(frozen=True)
class _Setting:
    key: str
    env: str | None


def test_a_new_environment_import_outside_migrated_settings_fails() -> None:
    settings = [*SETTINGS, _Setting("streams.max_per_user", "AGENT_MAX_STREAMS_PER_USER")]
    assert unmigrated_registrations(settings, MIGRATED_SETTINGS) == [
        "AGENT_MAX_STREAMS_PER_USER is a new environment import outside MIGRATED_SETTINGS: "
        "an existing database would never read it. Register it in MIGRATED_SETTINGS"
    ]
    assert unmigrated_registrations(settings, [*MIGRATED_SETTINGS, settings[-1]]) == []
    # A setting without an environment name imports nothing and may go anywhere.
    assert unmigrated_registrations([*SETTINGS, _Setting("x", None)], MIGRATED_SETTINGS) == []


def test_operator_templates_cannot_offer_internal_names() -> None:
    templates = [
        TemplateName("deploy/host/.env.example", "example", "AGENT_MCP_TOKEN"),
        TemplateName("deploy/control-plane/docker-compose.yml", "compose", "LOCAL_STACK_ID"),
        TemplateName("deploy/host/docker-compose.sandbox.yml", "container", "TUNNEL_SSH_PORT"),
    ]
    reads = _reads('import os\nos.environ.get("TEST_DB_HOST")\n')
    assert misplaced(Scan(reads, templates)) == [
        "deploy/host/.env.example lets an operator set AGENT_MCP_TOKEN, which this code sets",
        "deploy/control-plane/docker-compose.yml names the tooling variable LOCAL_STACK_ID",
        "apps/backend/cloud_agent/new.py:2 reads the tooling variable TEST_DB_HOST",
    ]


def test_registry_and_exception_entries_must_still_match(scan: Scan) -> None:
    trimmed = [
        read
        for read in scan.reads
        if not (read.module.endswith("domain_cipher.py") and read.function == "settings_root_key")
    ]
    assert any(
        "settings_root_key no longer reads AGENT_SESSION_SECRET" in p
        for p in unused_exceptions(trimmed)
    )


def test_a_new_literal_shell_input_must_be_classified() -> None:
    script = (
        'SHA="${1:-${DEPLOY_SHA:-}}"\n'
        'TIMEOUT="${AGENT_DEPLOY_NEW_TIMEOUT:-30}"\n'
        'env_or_dotenv() { local name="$1"; printf \'%s\' "${!name}"; }\n'
        'slots="$(env_or_dotenv AGENT_HOST_SLOTS)"\n'
        'token="$(env_or_dotenv AGENT_NEW_HOST_TOKEN)"\n'
    )
    shell, dynamic = scan_scripts({"deploy/host/new.sh": script})
    problems = unclassified(Scan([], [], shell, dynamic))
    assert [problem.split(":")[0] for problem in problems] == [
        "AGENT_DEPLOY_NEW_TIMEOUT",
        "AGENT_NEW_HOST_TOKEN",
    ]


def test_ordinary_shell_locals_are_not_inputs() -> None:
    script = (
        'log() { local message="$1"; echo "$message" >&2; }\n'
        "count=0; for item in a b; do count=$((count + 1)); done\n"
        'while IFS= read -r line; do log "$line"; done < /dev/null\n'
        'stack_name="${stack_name:-}"\n'
    )
    shell, dynamic = scan_scripts({"deploy/host/locals.sh": script})
    # `stack_name` is read guarded on its own first assignment: an input.
    assert [read.name for read in shell] == ["stack_name"]
    assert dynamic == []
    assert unclassified(Scan([], [], shell)) == [
        "stack_name: deploy/host/locals.sh:4 (shell defaulted)"
    ]


def test_shell_exceptions_cover_only_their_own_read() -> None:
    sourced = 'requested="${SHA:-}"; other="${ANOTHER_CALLER_VALUE:-}"\n'
    shell, _ = scan_scripts(
        {"deploy/release/artifact_env.sh": sourced, "deploy/host/other.sh": sourced}
    )
    problems = unclassified(Scan([], [], shell))
    # SHA is excused in artifact_env.sh only; the new name nowhere.
    assert [problem.split(":")[0] for problem in problems] == ["ANOTHER_CALLER_VALUE", "SHA"]
    assert "artifact_env.sh" in problems[0]
    assert "artifact_env.sh" not in problems[1]
    computed = 'name="$(pick)"; echo "${!name}"\n'
    _, dynamic = scan_scripts({"deploy/host/dynamic.sh": computed})
    assert (
        "deploy/host/dynamic.sh:1 <script> reads a variable it computes (${!name}): read a "
        "literal name, or add it to SHELL_DYNAMIC_READS"
    ) in shell_problems(Scan([], [], [], dynamic))


def test_a_shell_exception_nothing_needs_fails(scan: Scan) -> None:
    shell = [read for read in scan.shell if read.name != "SHA"]
    assert shell_problems(Scan([], [], shell, [], scan.shell_functions)) == [
        "SHELL_NOT_INPUTS entry ('deploy/release/artifact_env.sh', 'SHA') matches no read now"
    ]
    assert shell_problems(Scan([], [], scan.shell, [], frozenset())) == [
        "SHELL_HELPERS names artifact_env_value, which no script defines now"
    ]
