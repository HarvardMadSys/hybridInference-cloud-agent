"""The runtime pins and deploy-time version menus are complete and exact.

The five defaults are a source-level shared-protocol contract. Deployments may
add tested versions to each image's menu, but cannot replace one host's default
while the runner and control plane still attest the reviewed values. Three
things have to stay true, and each otherwise fails silently:

* **Complete.** Every extra-version argument is passed through by
  `sandbox_host.sh`; the reverse, a pass-through for an `ARG` that no longer
  exists, is configuration that quietly does nothing.
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
import subprocess
from pathlib import Path

from cloud_agent_host.runtimes import registered_runtimes

REPO = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO / "deploy/sandbox/Dockerfile.agent-sandbox"
SANDBOX_HOST = REPO / "deploy/host/sandbox_host.sh"

#: `ARG NAME_VERSION=1.2.3` — a pin, with the default that makes it optional.
_ARG_PIN = re.compile(r"^ARG ([A-Z0-9_]+_VERSION)=(\S+)$", re.MULTILINE)

#: `ARG NAME_EXTRA_VERSIONS=` — the menu's optional tail, empty by default.
_ARG_EXTRA = re.compile(r"^ARG ([A-Z0-9_]+_EXTRA_VERSIONS)=$", re.MULTILINE)

#: `ARG NAME_VERSIONS=${NAME_VERSION}...` — the derived menu behind each label.
_ARG_MENU = re.compile(r"^ARG ([A-Z0-9_]+_VERSIONS)=\$\{", re.MULTILINE)

#: `NAME="$(env_or_dotenv NAME)"` — the script's explicit configuration reads.
_SCRIPT_READ = re.compile(r"env_or_dotenv ([A-Z0-9_]+_VERSIONS?)\)")

#: Everything between `for name in` and `; do`. Matched as one span rather than
#: as a list of names so that reformatting the loop cannot quietly shrink what
#: this test believes it covers. The script has more than one loop of this
#: shape — `require_env` iterates credentials — so the caller picks the version
#: one by content rather than by position.
_PASS_THROUGH = re.compile(r"for name in ([A-Z0-9_ \t\\\n]+); do")


def _passed_to_docker_build(script: str) -> set[str] | None:
    """The names the `--build-arg` loop iterates, or None if it is gone."""
    for names in _PASS_THROUGH.findall(script):
        if "_VERSION" in names:
            # The loop wraps, so the span carries line continuations.
            return set(names.replace("\\", " ").split())
    return None


#: `check claude "${CLAUDE_CODE_VERSION}"` at the end of the image build.
_VERIFIED = re.compile(r'check \S+ "\$\{([A-Z0-9_]+_VERSION)\}"')

#: `install-runtimes.sh <id> <pkg> <bin> "${NAME_VERSIONS}"` — the install call.
_INSTALL = re.compile(r'install-runtimes\.sh \S+ \S+ \S+ "\$\{([A-Z0-9_]+_VERSIONS)\}"')


def _pins() -> dict[str, str]:
    return dict(_ARG_PIN.findall(DOCKERFILE.read_text()))


def _extras() -> set[str]:
    return set(_ARG_EXTRA.findall(DOCKERFILE.read_text()))


def _configurable() -> set[str]:
    """Every build argument a host deployment is allowed to set."""
    return _extras()


def test_every_pin_has_a_reviewed_default() -> None:
    """`docker build` with no arguments must still produce the shipped image."""
    pins = _pins()
    assert pins, "no version pins found in the Dockerfile — regex drift?"
    for name, default in pins.items():
        assert default, f"{name} has an empty default"


def test_the_deploy_script_passes_through_exactly_the_extra_version_menus() -> None:
    """A menu setting the deploy cannot pass is configuration silently lost."""
    script = SANDBOX_HOST.read_text()
    read = set(_SCRIPT_READ.findall(script))
    passed = _passed_to_docker_build(script)
    assert passed is not None, "sandbox_host.sh has no --build-arg pass-through loop"

    assert read == _configurable(), (
        "deploy/host/sandbox_host.sh reads "
        f"{sorted(read)} but the Dockerfile offers {sorted(_configurable())}"
    )
    assert passed == read, (
        f"sandbox_host.sh reads {sorted(read)} but only passes {sorted(passed)} "
        "to docker build — the rest are read and dropped"
    )


def test_every_pin_is_verified_against_the_installed_binary() -> None:
    """Nothing else stops a dist-tag turning the version labels into fiction."""
    verified = set(_VERIFIED.findall(DOCKERFILE.read_text()))
    assert verified == set(_pins()), (
        f"the image build verifies {sorted(verified)} but pins {sorted(_pins())}; "
        "an unverified pin can be a range or a dist-tag, and its label would "
        "then describe a binary the image does not contain"
    )


def test_the_example_offers_every_deploy_time_menu_and_no_default_override() -> None:
    """An operator learns these exist by reading the example, or not at all."""
    example = (REPO / "deploy/host/.env.example").read_text()
    assigned = {
        match.group(1)
        for line in example.splitlines()
        if (match := re.match(r"\s*([A-Z0-9_]+_VERSIONS?)=", line))
    }
    assert assigned == _configurable(), (
        f"deploy/host/.env.example offers {sorted(assigned)}, but host deployments "
        f"accept {sorted(_configurable())}"
    )


# ── What the script actually runs ────────────────────────────────────────
#
# The checks above read files. These run `sandbox_host.sh up` against a fake
# docker and read back the argv it was handed, because "the name appears in
# the script" and "the build receives the argument" are different claims and
# only the second one is the feature.


_BASE_ENV = "\n".join(
    [
        "COMPOSE_PROJECT=freeinference-cloud-agent-sandbox-staging",
        "AGENT_ENROLL_TOKEN=test-enrollment-token",
        "AGENT_SANDBOX_IMAGE=freeinference-cloud-agent-job:staging",
        "AGENT_TUNNEL_SSH_DESTINATION=user@gateway",
        "AGENT_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "AGENT_MCP_TUNNEL_SSH_DESTINATION=user@control",
        "AGENT_MCP_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION=user@control",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY=/dev/null",
        "AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS=/etc/hosts",
        "AGENT_BROKER_REVERSE_BIND=172.24.0.1",
        "AGENT_BROKER_REVERSE_PORT=18092",
        "AGENT_WORKSPACE_BROKER_ADVERTISE_URL=http://172.24.0.1:18092",
        "AGENT_WORKSPACE_BROKER_TOKEN=test-broker-token",
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
    for name in ("COMPOSE_PROJECT", *_pins(), *_extras()):
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

    assert "compose-env image=freeinference-cloud-agent-job:staging" in invocations


def test_an_extra_version_in_dotenv_reaches_docker_build(tmp_path: Path) -> None:
    """The deployment-supported version menu reaches the image build."""
    build = _build_line(_up(tmp_path, _BASE_ENV + "CLAUDE_CODE_EXTRA_VERSIONS=9.9.9\n"))

    assert "--build-arg CLAUDE_CODE_EXTRA_VERSIONS=9.9.9" in build
    # And only that one. A menu nobody set must not be passed as empty, which
    # would override the Dockerfile default with nothing and install `pkg@`.
    assert build.count("--build-arg") == 1, build


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
            **dict.fromkeys(set(_pins()) | _extras(), ""),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "source-level shared protocol pin" in result.stderr
    assert not any(line.startswith("build ") for line in docker_log.read_text().splitlines())


# ── The menu ─────────────────────────────────────────────────────────────


def test_every_runtime_offers_a_menu_derived_from_its_pin() -> None:
    """A label may only advertise versions the same build actually installed.

    `.versions` is what the admin page offers and what the claim validates a
    selection against. Derived from the pin plus that runtime's extras, and
    never passed in directly, so there is no build in which the menu names a
    version the install loop never saw.
    """
    dockerfile = DOCKERFILE.read_text()
    menus = set(_ARG_MENU.findall(dockerfile))

    expected = {f"{name}S" for name in _pins()}
    assert menus == expected, f"menus {sorted(menus)} do not match the pins {sorted(expected)}"

    for pin in _pins():
        menu = re.search(rf"^ARG {pin}S=(\S+)$", dockerfile, re.MULTILINE)
        assert menu, f"no menu derived for {pin}"
        # Default first: `install-runtimes.sh` symlinks the head of the list
        # onto PATH, so a menu that led with the extras would silently change
        # what a job with no selection runs.
        assert menu.group(1).startswith("${" + pin + "}"), (
            f"{pin}S must start with the pin — the first entry is the default"
        )
        assert f"{pin[: -len('_VERSION')]}_EXTRA_VERSIONS" in menu.group(1)


def test_the_install_reads_the_menu_not_the_pin() -> None:
    """Installing the pin alone would leave the extras advertised and absent."""
    installed = set(_INSTALL.findall(DOCKERFILE.read_text()))
    assert installed == {f"{name}S" for name in _pins()}, (
        f"install-runtimes.sh is called with {sorted(installed)}; every runtime "
        "must be installed from its menu, or a label offers a version no job can run"
    )


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
