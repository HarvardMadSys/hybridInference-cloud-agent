"""The runtime pins are the whole of what a sandbox image installs, and exact.

The five defaults are a source-level shared-protocol contract, and the image
carries exactly them: another version of an agent is downloaded at runtime
from Admin → Settings → Agent versions, never baked in by one host's build.
Three things have to stay true, and each otherwise fails silently:

* **Not overridable.** `sandbox_host.sh` passes no runtime build argument and
  refuses a per-host default, so no host can build an image whose pins differ
  from the ones the runner and control plane attest.
* **Defaulted.** The reviewed version stays in the Dockerfile beside the event
  parser written against it. An `ARG` without a default would make the image
  depend on a value only one caller supplies, and `docker build` on its own —
  which is what CI does — would produce an image with no CLI in it.
* **Verified.** Every pin is checked against the binary at build time. The
  labels are derived from these arguments, and the whole version-stamp
  pipeline (heartbeat → attempt record → native-session admission) treats a label as
  fact. An unverified pin lets `latest` produce an image whose label is a lie.

Read from the files rather than duplicated here on purpose: a list of the five
runtimes copied into this test would be a sixth place to forget.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path

import pytest

from cloud_agent_host.runtimes import registered_runtimes

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "deploy/sandbox/Dockerfile.agent-sandbox"
SANDBOX_HOST = REPO / "deploy/host/sandbox_host.sh"
INSTALL_RUNTIMES = REPO / "deploy/sandbox/install-runtimes.sh"

#: `ARG NAME_VERSION=1.2.3` — a pin, with the default that makes it optional.
_ARG_PIN = re.compile(r"^ARG ([A-Z0-9_]+_VERSION)=(\S+)$", re.MULTILINE)

#: `ARG NAME_VERSIONS=...` — a menu argument a build could set apart from its pin.
_ARG_MENU = re.compile(r"^ARG ([A-Z0-9_]+_VERSIONS)=", re.MULTILINE)

#: `NAME="$(env_or_dotenv NAME)"` — a configuration read of a runtime version.
_SCRIPT_READ = re.compile(r"env_or_dotenv ([A-Z0-9_]+_VERSIONS?)\)")

#: `check claude "${CLAUDE_CODE_VERSION}"` at the end of the image build.
_VERIFIED = re.compile(r'check \S+ "\$\{([A-Z0-9_]+_VERSION)\}"')

#: `install-runtimes.sh <id> <pkg> <bin> "${NAME_VERSION}"` — the install call.
_INSTALL = re.compile(r'install-runtimes\.sh \S+ \S+ \S+ "\$\{([A-Z0-9_]+_VERSIONS?)\}"')


def _pins() -> dict[str, str]:
    return dict(_ARG_PIN.findall(DOCKERFILE.read_text()))


def test_the_deploy_script_passes_no_runtime_version_to_the_build() -> None:
    """A version only one host's build received is one no other side attests."""
    script = SANDBOX_HOST.read_text()

    assert _SCRIPT_READ.findall(script) == [], (
        "deploy/host/sandbox_host.sh reads a runtime version from the host's "
        "configuration; the image's versions are the Dockerfile's reviewed pins"
    )
    assert "--build-arg" not in script, (
        "deploy/host/sandbox_host.sh passes a build argument to the sandbox image"
    )


def test_every_pin_is_verified_against_the_installed_binary() -> None:
    """Nothing else stops a dist-tag turning the version labels into fiction."""
    verified = set(_VERIFIED.findall(DOCKERFILE.read_text()))
    assert verified == set(_pins()), (
        f"the image build verifies {sorted(verified)} but pins {sorted(_pins())}; "
        "an unverified pin can be a range or a dist-tag, and its label would "
        "then describe a binary the image does not contain"
    )


# ── What the script actually runs ────────────────────────────────────────
#
# The checks above read files. These run `sandbox_host.sh up` against a fake
# docker and read back the argv it was handed, because "the name appears in
# the script" and "the build receives the argument" are different claims and
# only the second one is the feature.


_BASE_ENV = "\n".join(
    [
        "COMPOSE_PROJECT=cloud-agent-sandbox-staging",
        "AGENT_ENROLL_TOKEN=test-enrollment-token",
        "AGENT_SANDBOX_IMAGE=cloud-agent-job:staging",
        "AGENT_MCP_TUNNEL_SSH_DESTINATION=user@control",
        "AGENT_MCP_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION=user@control",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "AGENT_SANDBOX_MANAGER_REVERSE_BIND=172.24.0.1",
        "AGENT_SANDBOX_MANAGER_REVERSE_PORT=18092",
        "AGENT_SANDBOX_MANAGER_ADVERTISE_URL=http://172.24.0.1:18092",
        "AGENT_SANDBOX_MANAGER_TOKEN=test-sandbox-manager-token",
        "AGENT_EGRESS_CHAT_NETWORK_POOL=10.240.0.0/16",
        "AGENT_EGRESS_DNS_SERVER=1.1.1.1",
        # Skips the Kata preflight, which no developer machine passes. The
        # script warns loudly and continues, which is the documented behaviour
        # of this override and not something this test is asserting about.
        "AGENT_SANDBOX_BACKEND=container",
        "",
    ]
)


def _up(tmp_path: Path, dotenv: str) -> str:
    """Run `up` with a recording docker; return every argv it received.

    The fake docker fails the first `compose` call on purpose. Everything under
    test here — reading the pins, and handing them to `docker build` — happens
    before that, and stopping there skips the bring-up's five-second settle.
    """
    app_dir = tmp_path / "app"
    app_dir.mkdir(exist_ok=True)
    (app_dir / ".env").write_text(dotenv)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'case " $* " in *" config --environment "*) cat >/dev/null; printf "%s\\n" "$COMPOSE_ENVIRONMENT"; exit 0 ;; esac\n'
        'if [ "$*" = "info --format {{.DockerRootDir}}" ]; then\n'
        '  printf "/var/lib/docker\\n"\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = compose ]; then\n'
        '  printf "compose-env image=%s\\n" "${AGENT_SANDBOX_IMAGE:-}" >> "$DOCKER_LOG"\n'
        "  exit 1\n"
        "fi\n"
        "exit 0\n"
    )
    docker.chmod(0o755)

    log = tmp_path / "docker.log"
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "APP_DIR": str(app_dir),
        "DOCKER_LOG": str(log),
        "COMPOSE_ENVIRONMENT": dotenv,
        "AGENT_DEPLOYMENT_SHA": "test-deployment-sha",
        "AGENT_HOST_STATE_ROOT": str(tmp_path / "host-state"),
    }
    # Absence, not exported emptiness: Compose correctly gives even an empty
    # shell value precedence over the dotenv file.
    for name in ("COMPOSE_PROJECT", *_pins()):
        environment.pop(name, None)
    subprocess.run(
        ["bash", str(SANDBOX_HOST), "up"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    return log.read_text() if log.exists() else ""


def _build_line(invocations: str) -> str:
    for line in invocations.splitlines():
        if line.startswith("build "):
            return line
    raise AssertionError(f"docker build was never invoked; got:\n{invocations}")


def test_no_override_builds_the_reviewed_image(tmp_path: Path) -> None:
    """An unset pin passes no argument, so the Dockerfile default stands.

    This is the shape every existing host is in, and it has to keep producing
    exactly the image it produced before the pins were configurable.
    """
    build = _build_line(_up(tmp_path, _BASE_ENV))

    assert "--build-arg" not in build, (
        f"an unconfigured host passed a build argument anyway: {build}"
    )


def test_compose_receives_the_built_image(tmp_path: Path) -> None:
    invocations = _up(tmp_path, _BASE_ENV)

    assert "compose-env image=cloud-agent-job:staging" in invocations


def test_a_per_host_default_override_is_refused_before_build(tmp_path: Path) -> None:
    """A mismatched default would build, then lose shared-live capability."""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / ".env").write_text(_BASE_ENV + "CLAUDE_CODE_VERSION=9.9.9\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
        'case " $* " in *" config --environment "*) cat >/dev/null; cat .env; exit 0 ;; esac\n'
        'if [ "$*" = "info --format {{.DockerRootDir}}" ]; then echo /var/lib/docker; fi\n'
        "exit 0\n"
    )
    docker.chmod(0o755)

    result = subprocess.run(
        ["bash", str(SANDBOX_HOST), "up"],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "APP_DIR": str(app_dir),
            "DOCKER_LOG": str(docker_log),
            "AGENT_DEPLOYMENT_SHA": "test-deployment-sha",
            "AGENT_HOST_STATE_ROOT": str(tmp_path / "host-state"),
            "COMPOSE_PROJECT": "",
            **dict.fromkeys(_pins(), ""),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "CLAUDE_CODE_VERSION is a source-level shared protocol pin" in result.stderr
    # The refusal names both ways forward: the reviewed source change, and the
    # runtime update that needs no rebuild at all.
    assert "update the Dockerfile and SHARED_SANDBOX_RUNTIME_PINS together" in result.stderr
    assert "Admin -> Settings -> Agent versions" in result.stderr
    assert "EXTRA_VERSIONS" not in result.stderr
    assert not any(line.startswith("build ") for line in docker_log.read_text().splitlines())


@pytest.mark.parametrize(
    "versions",
    ["2.1", "latest", "^2.1.220", "~2.1.220", "2.1.x", "v2.1.220", "2.1.220 ", "2.1.220+b1"],
)
def test_the_runtime_install_refuses_a_version_that_is_not_exact(
    tmp_path: Path, versions: str
) -> None:
    """Refused before npm runs, so nothing is installed under a misleading label."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    npm_log = tmp_path / "npm.log"
    npm = fake_bin / "npm"
    npm.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$NPM_LOG"\nexit 1\n')
    npm.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_RUNTIMES),
            "claude-code",
            "@anthropic-ai/claude-code",
            "claude",
            versions,
        ],
        env={**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}", "NPM_LOG": str(npm_log)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "is not an exact version" in result.stderr
    assert not npm_log.exists()


def _install_version_match() -> str:
    """``reports_version`` exactly as install-runtimes.sh defines it."""
    script = INSTALL_RUNTIMES.read_text()
    start = script.index("reports_version() {")
    return script[start : script.index("\n}\n", start) + 3]


def _image_version_check() -> str:
    """The image's final ``check`` function, with its line continuations undone."""
    dockerfile = DOCKERFILE.read_text()
    start = dockerfile.index("    check() { \\\n")
    end = dockerfile.index("    }; \\\n", start) + len("    }; \\\n")
    return "\n".join(line.removesuffix(" \\") for line in dockerfile[start:end].splitlines())


@pytest.mark.parametrize(
    ("reported", "version", "matches"),
    [
        ("2.1.220 (Claude Code)", "2.1.220", True),
        ("codex-cli 0.145.0", "0.145.0", True),
        ("0.73.1", "0.73.1", True),
        ("v1.18.9", "1.18.9", True),
        ("2.1.220 (Claude Code)", "2.1", False),
        ("2.1.220 (Claude Code)", "1.220", False),
        ("codex-cli 10.145.0", "0.145.0", False),
        ("codex-cli 0.145.0-alpha.1", "0.145.0", False),
        ("1.18.90", "1.18.9", False),
    ],
)
def test_a_binary_must_report_its_version_as_a_whole_token(
    tmp_path: Path, reported: str, version: str, matches: bool
) -> None:
    """A substring match took `2.1` from `2.1.220` and labelled a range exact."""
    binary = tmp_path / "cli"
    binary.write_text(f"#!/bin/sh\necho 'a banner line'\necho {shlex.quote(reported)}\n")
    binary.chmod(0o755)

    by_install = subprocess.run(
        [
            "bash",
            "-c",
            _install_version_match() + 'reports_version "$1" "$2"',
            "-",
            reported,
            version,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    by_image = subprocess.run(
        ["sh", "-c", _image_version_check() + '\ncheck "$1" "$2"', "-", str(binary), version],
        capture_output=True,
        text=True,
        check=False,
    )

    assert (by_install.returncode == 0) is matches, by_install.stderr
    assert (by_image.returncode == 0) is matches, by_image.stderr


# ── The menu ─────────────────────────────────────────────────────────────


def test_the_image_has_no_menu_argument_apart_from_its_pins() -> None:
    """A menu a build could set separately would label versions nobody reviewed."""
    menus = _ARG_MENU.findall(DOCKERFILE.read_text())
    assert menus == [], (
        f"the sandbox Dockerfile declares {sorted(menus)}; the image installs and "
        "labels its pins alone"
    )


def test_the_install_reads_the_pin() -> None:
    """Every runtime is installed at exactly its reviewed pin."""
    installed = set(_INSTALL.findall(DOCKERFILE.read_text()))
    assert installed == set(_pins()), (
        f"install-runtimes.sh is called with {sorted(installed)}; every runtime "
        f"must be installed from its pin {sorted(_pins())}"
    )


def test_every_menu_label_is_its_pin() -> None:
    """`.versions` names what the image holds, and it holds only the pin."""
    dockerfile = DOCKERFILE.read_text()

    def labelled(suffix: str) -> dict[str, str]:
        pattern = rf'org\.freeinference\.runtime\.([a-z0-9-]+)\.{suffix}="\$\{{([A-Z0-9_]+)\}}"'
        return dict(re.findall(pattern, dockerfile))

    defaults = labelled("version")
    assert set(defaults.values()) == set(_pins()), defaults
    assert labelled("versions") == defaults


def test_every_menu_is_labelled() -> None:
    """An unlabelled menu is a host that reports one option and holds several."""
    dockerfile = DOCKERFILE.read_text()
    for runtime_id in registered_runtimes():
        for suffix in ("version", "versions"):
            assert re.search(
                rf"org\.freeinference\.runtime\.{re.escape(runtime_id)}\.{suffix}="
                r'"\$\{[A-Z0-9_]+\}"',
                dockerfile,
            ), f"{runtime_id} has no .{suffix} label"
