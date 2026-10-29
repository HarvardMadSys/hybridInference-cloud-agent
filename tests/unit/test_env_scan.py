"""The environment scan sees every spelling the classification gate depends on.

A guard that quietly matches nothing is worse than none, so each read form the
repository uses has a case here, and so does each form the scan reports as
unnamed rather than guessing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.support.env_scan import EnvRead, find_env_reads, scan_paths, template_names

if TYPE_CHECKING:
    from pathlib import Path

_EVERY_SPELLING = """
import os
import os as operating_system
from os import environ, getenv
from os import getenv as fetch

from cloud_agent.settings import setting_env, settings_environment
from cloud_agent_shared.sandbox_manager_compat import manager_env, manager_environment
from cloud_agent_shared.profiling import schema

NAME = "AGENT_CONSTANT"
ALIAS = NAME
GROUP = ("AGENT_GROUP_A", "AGENT_GROUP_B")
_get = os.getenv


def reads(name):
    os.getenv("AGENT_GETENV")
    os.environ["AGENT_SUBSCRIPT"]
    os.environ.get("AGENT_ENVIRON_GET", "")
    getenv("AGENT_FROM_IMPORT")
    fetch("AGENT_IMPORT_ALIAS")
    environ.get("AGENT_ENVIRON_IMPORT")
    operating_system.getenv("AGENT_MODULE_ALIAS")
    _get(ALIAS)
    os.environ.setdefault("AGENT_SETDEFAULT", "1")
    value = os.environ.pop("AGENT_POPPED")
    "AGENT_MEMBERSHIP" in os.environ
    for key in GROUP:
        os.environ.get(key)
    list(map(os.environ.get, ("AGENT_MAPPED",)))
    os.environ.get(schema.ENV_PROFILING_SINK)
    manager_env("AGENT_COMPAT")
    setting_env("AGENT_RESOLVED")
    os.environ["AGENT_WRITTEN"] = "not a read"
    os.environ.update({"AGENT_ALSO_WRITTEN": "1"})
    os.environ.pop("AGENT_REMOVED", None)
    return value


def mappings(env=None):
    source = env if env is not None else dict(os.environ)
    source.get("AGENT_FROM_MAPPING")
    (env if env is not None else os.environ).get("AGENT_INLINE_MAPPING")
    resolved = settings_environment()
    resolved["AGENT_RESOLVED_MAPPING"]
    legacy = manager_environment(os.environ)
    "AGENT_COMPAT_MAPPING" in legacy


def _env_float(name, fallback):
    raw = os.environ.get(name)
    return float(raw) if raw else fallback


def helpers():
    _env_float("AGENT_THROUGH_HELPER", 1.0)


class Provider:
    def __init__(self, env=None):
        self._env = dict(env) if env is not None else settings_environment()

    def refresh(self):
        return self._env.get("AGENT_SELF_MAPPING")


def unnamed(setting):
    os.getenv(setting.env)
    os.environ.get("AGENT_" + setting.key)
    patterns = [v for k, v in os.environ.items() if k.startswith("AGENT_PATTERN_")]
    subprocess.run(["true"], env={**os.environ, "EXTRA": "1"})
    if env is not None:
        pass
"""


def _named(reads: list[EnvRead]) -> list[tuple[str, str]]:
    return [(name, read.via) for read in reads if read.names for name in read.names]


def test_the_scan_sees_every_spelling() -> None:
    constants = {"cloud_agent_shared.profiling.schema": {"ENV_PROFILING_SINK": ("AGENT_SINK",)}}
    imported = {
        "cloud_agent_shared.profiling.schema": constants["cloud_agent_shared.profiling.schema"]
    }
    reads = find_env_reads(_EVERY_SPELLING, "example.py", imported)

    assert sorted(_named(reads)) == sorted(
        [
            ("AGENT_GETENV", "raw"),
            ("AGENT_SUBSCRIPT", "raw"),
            ("AGENT_ENVIRON_GET", "raw"),
            ("AGENT_FROM_IMPORT", "raw"),
            ("AGENT_IMPORT_ALIAS", "raw"),
            ("AGENT_ENVIRON_IMPORT", "raw"),
            ("AGENT_MODULE_ALIAS", "raw"),
            ("AGENT_CONSTANT", "raw"),
            ("AGENT_SETDEFAULT", "raw"),
            ("AGENT_POPPED", "raw"),
            ("AGENT_MEMBERSHIP", "raw"),
            ("AGENT_GROUP_A", "raw"),
            ("AGENT_GROUP_B", "raw"),
            ("AGENT_MAPPED", "raw"),
            ("AGENT_SINK", "raw"),
            ("AGENT_COMPAT", "compat"),
            ("AGENT_RESOLVED", "resolver"),
            ("AGENT_FROM_MAPPING", "raw"),
            ("AGENT_INLINE_MAPPING", "raw"),
            ("AGENT_RESOLVED_MAPPING", "resolver"),
            ("AGENT_COMPAT_MAPPING", "compat"),
            ("AGENT_THROUGH_HELPER", "raw"),
            ("AGENT_SELF_MAPPING", "resolver"),
            ("AGENT_PATTERN_*", "raw"),
        ]
    )
    written = {"AGENT_WRITTEN", "AGENT_ALSO_WRITTEN", "AGENT_REMOVED"}
    assert not written & {name for name, _via in _named(reads)}


def test_what_the_scan_cannot_name_is_reported_not_guessed() -> None:
    reads = find_env_reads(_EVERY_SPELLING, "example.py")
    unnamed = [(read.function, read.via, read.argument) for read in reads if read.names is None]
    assert ("unnamed", "raw", "setting.env") in unnamed
    assert ("unnamed", "raw", "'AGENT_' + setting.key") in unnamed
    assert any(function == "unnamed" and via == "copy" for function, via, _ in unnamed)
    # The helper's own read is explained by its callers, not left unnamed.
    assert ("_env_float", "helper", "name") in unnamed
    # `env is not None` and `dict(os.environ)` bound to a mapping are not copies.
    assert [read for read in reads if read.function == "mappings" and read.via == "copy"] == []


def test_a_mapping_handed_to_another_module_is_followed(tmp_path: Path) -> None:
    """``build_backend_from_env(env)`` -> ``network_from_env(source)`` -> ``source.get``."""
    package = tmp_path / "apps" / "backend" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "network.py").write_text(
        'def network_from_env(source):\n    return source.get("AGENT_NETWORK_NAME_X")\n'
    )
    (package / "sandbox.py").write_text(
        "import os\n"
        "from pkg.network import network_from_env\n"
        "def build(env=None):\n"
        "    source = env if env is not None else dict(os.environ)\n"
        "    return network_from_env(source)\n"
        "def candidate():\n"
        '    return build({**os.environ, "AGENT_OVERRIDE": "x"})\n'
    )
    reads = scan_paths(
        sorted(package.glob("*.py")), root=tmp_path, package_root=tmp_path / "apps" / "backend"
    )
    assert [(read.module, read.names) for read in reads if read.names] == [
        ("apps/backend/pkg/network.py", ("AGENT_NETWORK_NAME_X",))
    ]
    # Handed to scanned code, so not a copy to account for.
    assert [read for read in reads if read.via == "copy"] == []


def test_an_embedded_script_is_scanned() -> None:
    source = "SCRIPT = r'''\nimport os, sys\npath = os.environ.pop('AGENT_SCRIPT_X')\n'''\n"
    reads = find_env_reads(source, "host.py")
    assert [(read.names, read.embedded) for read in reads] == [(("AGENT_SCRIPT_X",), True)]


def test_template_names_come_from_each_kind_of_file(tmp_path: Path) -> None:
    files = {
        "deploy/x/docker-compose.yml": (
            "services:\n"
            "  app:\n"
            "    build:\n"
            "      args:\n"
            "        NEXT_PUBLIC_X: ${AGENT_X_BUILD:-}\n"
            "    environment:\n"
            "      AGENT_X_KEY: ${AGENT_X_VALUE:-${AGENT_X_FORMER-}}\n"
        ),
        "deploy/x/.env.example": "AGENT_X_SET=1\n# AGENT_X_OPTIONAL=\nnot a key\n",
        "deploy/x/Dockerfile": "ARG AGENT_X_ARG=1\nRUN echo ${shell_local}\n",
        "apps/frontend/src/a.ts": "process.env.NEXT_PUBLIC_Y; process.env['AGENT_Y'];\n",
        "apps/frontend/src/a.test.ts": "process.env.IGNORED_IN_TESTS;\n",
        "scripts/run.sh": "echo $AGENT_SHELL_ONLY\n",
    }
    for name, text in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text)
    found = {(t.kind, t.name) for t in template_names(tmp_path, sorted(files))}
    assert found == {
        ("compose", "AGENT_X_BUILD"),
        ("compose", "AGENT_X_VALUE"),
        ("compose", "AGENT_X_FORMER"),
        ("container", "AGENT_X_KEY"),
        ("build_arg", "NEXT_PUBLIC_X"),
        ("example", "AGENT_X_SET"),
        ("example", "AGENT_X_OPTIONAL"),
        ("dockerfile_arg", "AGENT_X_ARG"),
        ("web", "NEXT_PUBLIC_Y"),
        ("web", "AGENT_Y"),
    }
