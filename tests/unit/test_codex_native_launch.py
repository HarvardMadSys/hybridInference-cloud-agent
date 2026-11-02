"""A Codex member starts its native binary exactly as the npm shim would have.

The launch script runs here against a private root: the one line naming the
image's install prefix points into a temporary directory laid out as
``npm install -g --prefix`` lays out a version, a reviewed shim's digest is
pointed at the stand-in shim's bytes, ``uname`` answers for the machine under
test, and every other line is the script the host sends. The stand-in shim and
binary report what they were started with.
"""

from __future__ import annotations

import hashlib
import json
import re
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from cloud_agent_host.compat_helper import helper_archive
from cloud_agent_host.runtimes import CodexRuntime, ResponsesViaChatUrl
from cloud_agent_host.runtimes.base import COMPAT_LAUNCHER
from cloud_agent_host.runtimes.codex import (
    NATIVE_LAUNCH,
    NATIVE_LAUNCHER,
    REVIEWED_SHIMS,
    ReviewedShim,
)
from cloud_agent_shared.shared_protocol import SHARED_SANDBOX_RUNTIME_PINS

#: The script runs in the Linux sandbox, with its coreutils.
_LINUX = pytest.mark.skipif(sys.platform != "linux", reason="the launch runs in the Linux sandbox")
_PREFIX_LINE = "CODEX_LAUNCH_PREFIX=/opt/runtimes/codex/\n"
_PINNED = SHARED_SANDBOX_RUNTIME_PINS["codex"]
#: Each machine's platform package and target triple, from the shims' own table.
_TARGETS = {
    "x86_64": ("x64", "x86_64-unknown-linux-musl"),
    "aarch64": ("arm64", "aarch64-unknown-linux-musl"),
}
_MANAGED = (
    "CODEX_MANAGED_BY_NPM",
    "CODEX_MANAGED_BY_BUN",
    "CODEX_MANAGED_BY_PNPM",
    "CODEX_MANAGED_BY_VITE_PLUS",
)
#: What each reviewed shim's ``bin/codex.js`` deletes from the environment it
#: inherited before setting its own flag, by the first release that shipped it.
_CLEARED = {
    "0.138.0": (),
    "0.144.0": _MANAGED[:3],
    "0.153.0": _MANAGED,
    "0.166.0": _MANAGED,
}


def _release(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _pinned_shim() -> ReviewedShim:
    pin = _release(_PINNED)
    return next(
        shim
        for shim in REVIEWED_SHIMS
        if _release(shim.releases[0]) <= pin <= _release(shim.releases[1])
    )


def _reporter(who: str, *, then: str = "exit") -> bytes:
    """A stand-in executable: it reports what it was started with as JSON, then runs ``then``."""
    report = (
        "import json, os, sys; print(json.dumps({'ran': sys.argv[1], 'pid': int(sys.argv[2]), "
        "'argv': sys.argv[3:], 'env': dict(os.environ)}), flush=True)"
    )
    return f'#!/bin/sh\n"{sys.executable}" -I -c "{report}" {who} "$$" "$0" "$@"\n{then}\n'.encode()


SHIM = _reporter("shim")


def _executable(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    path.chmod(0o755)


def _install(
    root: Path,
    version: str = _PINNED,
    *,
    machine: str = "x86_64",
    native: bytes | None = None,
    linked: bool = True,
    package_json: bool = True,
    prefix: str = "opt/runtimes/codex/{version}",
) -> Path:
    """Lay one version out as the image's npm install does; return its package root."""
    base = root / prefix.format(version=version)
    package = base / "lib/node_modules/@openai/codex"
    _executable(package / "bin/codex.js", SHIM)
    if linked:
        # npm links a global package's bin relatively, as on the image.
        (base / "bin").mkdir(parents=True)
        (base / "bin/codex").symlink_to("../lib/node_modules/@openai/codex/bin/codex.js")
    else:
        _executable(base / "bin/codex", SHIM)
    arch, triple = _TARGETS[machine]
    platform = package / f"node_modules/@openai/codex-linux-{arch}"
    platform.mkdir(parents=True)
    if package_json:
        (platform / "package.json").write_text('{"name": "@openai/codex"}\n')
    if native is not None:
        _executable(platform / f"vendor/{triple}/bin/codex", native)
    return package


def _make_default(root: Path, version: str = _PINNED) -> Path:
    """Link a version onto the image's default PATH, as install-runtimes.sh does."""
    bin_dir = root / "usr/local/bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "codex").symlink_to(root / f"opt/runtimes/codex/{version}/bin/codex")
    return bin_dir


def _launch(root: Path, *, reviewed: ReviewedShim | None = None) -> str:
    """The host's script, pointed at ``root`` and, for ``reviewed``, at the stand-in's bytes."""
    script = NATIVE_LAUNCH
    replacements = {_PREFIX_LINE: f"CODEX_LAUNCH_PREFIX={root}/opt/runtimes/codex/\n"}
    if reviewed is not None:
        replacements[f"{reviewed.sha256})"] = f"{hashlib.sha256(SHIM).hexdigest()})"
    for old, new in replacements.items():
        assert script.count(old) == 1, old
        script = script.replace(old, new)
    return script


def _environment(root: Path, *path: Path, machine: str = "x86_64", **extra: str) -> dict[str, str]:
    """A member-like environment whose ``uname -m`` reports ``machine``."""
    _executable(
        root / f"tools-{machine}/uname",
        f'#!/bin/sh\n[ "$*" = -m ] || exit 2\necho {machine}\n'.encode(),
    )
    return {
        "PATH": ":".join([*map(str, path), str(root / f"tools-{machine}"), "/usr/bin", "/bin"]),
        "HOME": str(root / "home"),
        "LANG": "C.UTF-8",
        "CODEX_API_KEY": "member-credential",
        "OWNER_SETTING": "kept",
        **extra,
    }


def _run(script: str, env: dict[str, str], *args: str) -> tuple[subprocess.Popen[str], dict]:
    """Start ``codex`` through ``script`` as the sandbox does; return its one report."""
    process = subprocess.Popen(
        ["/bin/sh", "-c", script, "codex", *args],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, stderr
    report = json.loads(stdout)
    # ``sh`` exports the working directory to whatever it starts, either way.
    report["env"].pop("PWD", None)
    return process, report


def _native(package: Path, machine: str = "x86_64") -> str:
    arch, triple = _TARGETS[machine]
    return f"{package}/node_modules/@openai/codex-linux-{arch}/vendor/{triple}/bin/codex"


@_LINUX
def test_the_pinned_install_starts_the_binary_the_shim_would_have(tmp_path):
    root = tmp_path.resolve()
    package = _install(root, native=_reporter("native"))
    env = _environment(root, root / f"opt/runtimes/codex/{_PINNED}/bin")
    argv, _ = CodexRuntime().prepare_live(
        workdir="/workspace",
        model="gpt-5.1-codex",
        model_base_url="https://provider.example/v1",
        credential="member-credential",
    )
    arguments = argv[len(NATIVE_LAUNCHER) + 1 :]

    process, report = _run(_launch(root, reviewed=_pinned_shim()), env, *arguments)

    assert report["ran"] == "native"
    # The same path and arguments the shim would have started it with...
    assert report["argv"] == [_native(package), *arguments]
    # ...in the process the sandbox started: no shell or Node in between.
    assert report["pid"] == process.pid
    # The inherited environment, ``PATH`` included, plus the shim's two.
    assert report["env"] == {
        **env,
        "CODEX_MANAGED_BY_NPM": "1",
        "CODEX_MANAGED_PACKAGE_ROOT": str(package),
    }


@pytest.mark.parametrize("machine", sorted(_TARGETS))
@_LINUX
def test_the_machine_picks_the_platform_package_and_triple(tmp_path, machine):
    root = tmp_path.resolve()
    package = _install(root, machine=machine, native=_reporter("native"))
    env = _environment(root, root / f"opt/runtimes/codex/{_PINNED}/bin", machine=machine)

    _, report = _run(_launch(root, reviewed=_pinned_shim()), env, "app-server")

    assert report["ran"] == "native"
    assert report["argv"][0] == _native(package, machine)


@_LINUX
def test_the_default_version_is_found_through_its_links(tmp_path):
    """No version on PATH: ``codex`` is the image default's link chain, resolved."""
    root = tmp_path.resolve()
    package = _install(root, native=_reporter("native"))
    env = _environment(root, _make_default(root))

    _, report = _run(_launch(root, reviewed=_pinned_shim()), env, "app-server")

    assert report["ran"] == "native"
    assert report["argv"][0] == _native(package)
    assert report["env"]["CODEX_MANAGED_PACKAGE_ROOT"] == str(package)


@_LINUX
def test_the_path_prefix_still_selects_the_version(tmp_path):
    root = tmp_path.resolve()
    _install(root, native=_reporter("native"))
    selected = _install(root, "0.150.0", native=_reporter("native"))
    default = _make_default(root)
    env = _environment(root, root / "opt/runtimes/codex/0.150.0/bin", default)

    _, report = _run(_launch(root, reviewed=_pinned_shim()), env, "app-server")

    assert report["argv"][0] == _native(selected)
    assert report["env"]["CODEX_MANAGED_PACKAGE_ROOT"] == str(selected)


def _assert_shim_ran(report: dict, env: dict[str, str], found: Path) -> None:
    """The shim ran as ``exec codex`` always ran it: ``found`` on PATH, unchanged."""
    assert report["ran"] == "shim"
    # The path PATH named, not the file behind it: the shim reads its own
    # entry point's directory, and a link resolves differently from there.
    assert report["argv"] == [str(found), "app-server"]
    assert report["env"] == env


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param({"native": None}, id="no binary"),
        pytest.param({"native": _reporter("native"), "package_json": False}, id="no package"),
        pytest.param({"native": _reporter("native"), "machine": "aarch64"}, id="other machine"),
        pytest.param(
            {"native": _reporter("native"), "prefix": "usr/local"}, id="older image install"
        ),
        # Copied out of its package, the shim would look for the binary
        # beside the copy and fail; it must still be the shim that runs.
        pytest.param({"native": _reporter("native"), "linked": False}, id="copied shim"),
    ],
)
@_LINUX
def test_a_layout_the_shim_would_not_resolve_the_same_way_runs_the_shim(tmp_path, layout):
    root = tmp_path.resolve()
    _install(root, **layout)
    version_bin = root / (
        "usr/local/bin" if layout.get("prefix") else f"opt/runtimes/codex/{_PINNED}/bin"
    )
    env = _environment(root, version_bin)

    _, report = _run(_launch(root, reviewed=_pinned_shim()), env, "app-server")

    _assert_shim_ran(report, env, version_bin / "codex")


@_LINUX
def test_a_binary_it_may_not_execute_runs_the_shim(tmp_path):
    root = tmp_path.resolve()
    package = _install(root, native=_reporter("native"))
    Path(_native(package)).chmod(0o644)
    version_bin = root / f"opt/runtimes/codex/{_PINNED}/bin"
    env = _environment(root, version_bin)

    _, report = _run(_launch(root, reviewed=_pinned_shim()), env, "app-server")

    _assert_shim_ran(report, env, version_bin / "codex")


@_LINUX
def test_an_unknown_machine_runs_the_shim(tmp_path):
    root = tmp_path.resolve()
    _install(root, native=_reporter("native"))
    version_bin = root / f"opt/runtimes/codex/{_PINNED}/bin"
    env = _environment(root, version_bin, machine="riscv64")

    _, report = _run(_launch(root, reviewed=_pinned_shim()), env, "app-server")

    _assert_shim_ran(report, env, version_bin / "codex")


@_LINUX
def test_a_shim_nobody_has_read_runs_as_it_always_has(tmp_path):
    """Every check passes but the digest: an unread shim might set something else."""
    root = tmp_path.resolve()
    _install(root, native=_reporter("native"))
    version_bin = root / f"opt/runtimes/codex/{_PINNED}/bin"
    env = _environment(root, version_bin)

    _, report = _run(_launch(root), env, "app-server")

    _assert_shim_ran(report, env, version_bin / "codex")


@_LINUX
def test_the_unchanged_script_runs_any_other_codex_by_name(tmp_path):
    """A host or test machine without the image's prefix starts its ``codex`` as before."""
    root = tmp_path.resolve()
    _executable(root / "elsewhere/codex", SHIM)
    env = _environment(root, root / "elsewhere")

    _, report = _run(NATIVE_LAUNCH, env, "app-server")

    _assert_shim_ran(report, env, root / "elsewhere/codex")


@pytest.mark.parametrize("reviewed", REVIEWED_SHIMS, ids=lambda shim: shim.releases[0])
@_LINUX
def test_each_shim_clears_exactly_the_flags_it_clears(tmp_path, reviewed):
    """An owner can never set these; the shims differ in them all the same."""
    root = tmp_path.resolve()
    _install(root, native=_reporter("native"))
    inherited = dict.fromkeys(_MANAGED, "inherited")
    env = _environment(root, root / f"opt/runtimes/codex/{_PINNED}/bin", **inherited)

    _, report = _run(_launch(root, reviewed=reviewed), env, "app-server")

    kept = {name: "inherited" for name in _MANAGED if name not in _CLEARED[reviewed.releases[0]]}
    assert {name: report["env"][name] for name in _MANAGED if name in report["env"]} == {
        **kept,
        "CODEX_MANAGED_BY_NPM": "1",
    }


@pytest.mark.parametrize(
    ("variables", "flag"),
    [
        pytest.param({}, "CODEX_MANAGED_BY_NPM", id="plain launch"),
        pytest.param(
            {"npm_config_user_agent": "npm/10.9.2 node/v22.22.0 linux x64"},
            "CODEX_MANAGED_BY_NPM",
            id="npm agent",
        ),
        pytest.param(
            {"npm_config_user_agent": "bun/1.3.14 npm/? node/v24.3.0 linux x64"},
            "CODEX_MANAGED_BY_BUN",
            id="bun agent",
        ),
        pytest.param(
            {"npm_config_user_agent": "pnpm/10 (bun/1.3)"},
            "CODEX_MANAGED_BY_BUN",
            id="bun after a non-word character",
        ),
        pytest.param(
            {"npm_config_user_agent": "notbun/1.0"},
            "CODEX_MANAGED_BY_NPM",
            id="bun inside a word",
        ),
        pytest.param(
            {"npm_execpath": "/home/agent/.bun/bin/bun"},
            "CODEX_MANAGED_BY_BUN",
            id="bun exec path",
        ),
    ],
)
@_LINUX
def test_the_package_manager_flag_follows_the_shims_own_test(tmp_path, variables, flag):
    root = tmp_path.resolve()
    package = _install(root, native=_reporter("native"))
    env = _environment(root, root / f"opt/runtimes/codex/{_PINNED}/bin", **variables)

    _, report = _run(_launch(root, reviewed=_pinned_shim()), env, "app-server")

    assert report["env"] == {**env, flag: "1", "CODEX_MANAGED_PACKAGE_ROOT": str(package)}


@_LINUX
def test_the_binary_owns_its_exit_status_and_signals(tmp_path):
    """Nothing forwards: a group signal or an exit status reaches the runner as the binary's."""
    root = tmp_path.resolve()
    _install(root, native=_reporter("native", then="exec sleep 30"))
    env = _environment(root, root / f"opt/runtimes/codex/{_PINNED}/bin")
    process = subprocess.Popen(
        ["/bin/sh", "-c", _launch(root, reviewed=_pinned_shim()), "codex", "app-server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        assert process.stdout is not None
        assert json.loads(process.stdout.readline())["pid"] == process.pid
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=30) == -signal.SIGTERM
    finally:
        process.kill()
        process.wait(timeout=30)

    _install(root, "0.150.0", native=_reporter("native", then="exit 3"))
    env = _environment(root, root / "opt/runtimes/codex/0.150.0/bin")
    exited = subprocess.run(
        ["/bin/sh", "-c", _launch(root, reviewed=_pinned_shim()), "codex", "app-server"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert json.loads(exited.stdout)["ran"] == "native"
    assert exited.returncode == 3


_DISPOSITION = re.compile(r"^(SigIgn|SigBlk):\t([0-9a-f]+)$", re.MULTILINE)
#: The two signals CPython ignores from its start.
_PYTHON_IGNORES = (1 << (signal.SIGPIPE - 1)) | (1 << (signal.SIGXFSZ - 1))


@_LINUX
def test_the_binary_starts_with_a_direct_launchs_signals_behind_the_helper_too(tmp_path):
    """On an owner's Chat Completions connection the helper's Python execs the launch.

    CPython starts with SIGPIPE and SIGXFSZ ignored, an exec keeps a signal
    ignored, and ``sh`` cannot take one back, so the helper must restore them.
    ``grep`` stands in for the binary and reads its own dispositions.
    """
    root = tmp_path.resolve()
    _install(root, native=b"#!/bin/sh\nexec grep -E '^Sig(Ign|Blk):' /proc/self/status\n")
    env = _environment(root, root / f"opt/runtimes/codex/{_PINNED}/bin")
    zipapp = root / "model-protocol-translator.pyz"
    zipapp.write_bytes(helper_archive())
    local = {
        NATIVE_LAUNCH: _launch(root, reviewed=_pinned_shim()),
        COMPAT_LAUNCHER[-1]: str(zipapp),
    }
    started = {}
    for plan, url in (
        ("direct", "https://provider.example/v1"),
        ("translated", ResponsesViaChatUrl("https://provider.example/v1")),
    ):
        argv, launch_env = CodexRuntime().prepare_live(
            workdir="/workspace", model="chat-model", model_base_url=url, credential="credential"
        )
        run = subprocess.run(
            [local.get(arg, arg) for arg in argv],
            env={**env, **launch_env, "AGENT_COMPAT_TOKEN": "t" * 64},
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert run.returncode == 0, run.stderr
        started[plan] = {name: int(mask, 16) for name, mask in _DISPOSITION.findall(run.stdout)}

    # Both reached the binary, and it started the same way behind the helper.
    assert set(started["direct"]) == {"SigIgn", "SigBlk"}
    assert started["translated"] == started["direct"]
    assert not started["direct"]["SigIgn"] & _PYTHON_IGNORES


def test_every_codex_launch_starts_with_the_native_launch():
    direct, _ = CodexRuntime().prepare_live(
        workdir="/workspace",
        model="gpt-5.1-codex",
        model_base_url="https://provider.example/v1",
        credential="member-credential",
    )
    translated, _ = CodexRuntime().prepare_live(
        workdir="/workspace",
        model="chat-model",
        model_base_url=ResponsesViaChatUrl("https://api.example.com/v1"),
        credential="provider-key",
    )

    assert direct[:4] == [*NATIVE_LAUNCHER, "codex"]
    # Behind the compatibility helper, which execs it in turn.
    assert translated[: len(COMPAT_LAUNCHER) + 4] == [*COMPAT_LAUNCHER, *NATIVE_LAUNCHER, "codex"]
    assert direct[-1] == translated[-1] == "app-server"


def test_the_pinned_version_ships_a_reviewed_shim():
    """Moving the pin past the reviewed shims means reading the new one first."""
    pin = _release(_PINNED)
    assert any(
        _release(first) <= pin <= _release(last)
        for first, last in (shim.releases for shim in REVIEWED_SHIMS)
    )


def test_the_reviewed_shims_are_distinct_and_ordered():
    digests = [shim.sha256 for shim in REVIEWED_SHIMS]
    assert all(re.fullmatch("[0-9a-f]{64}", digest) for digest in digests)
    assert len(set(digests)) == len(digests)
    bounds = [_release(version) for shim in REVIEWED_SHIMS for version in shim.releases]
    assert bounds == sorted(bounds)
    assert all(set(shim.clears) <= set(_MANAGED) for shim in REVIEWED_SHIMS)
