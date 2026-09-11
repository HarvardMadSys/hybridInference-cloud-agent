"""Artifact mode may not cost a fence, and may not gain a build.

Both stacks now have a second way to be deployed, and the failure worth a test
of its own is the quiet one: a mode that works while skipping the readiness
command, or that can build an image the release never pinned. Every string below
predates the split, and each is asserted by name so that making one conditional
is an argument somebody has to make rather than a line that slipped into a
branch.

This is the half of P3 that survived consolidation from the parallel
implementation: the two branches agreed on the mechanism and differed on what
was pinned, and this is what the other branch pinned.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL = (REPO_ROOT / "deploy/control-plane/deploy.sh").read_text()
HOST = (REPO_ROOT / "deploy/host/sandbox_host.sh").read_text()
HELPER = (REPO_ROOT / "deploy/release/artifact_env.sh").read_text()

#: The compatibility fences. Each one exists because something went wrong
#: without it, and none of them is a property of where the images came from.
FENCES = (
    'compose up -d --wait --wait-timeout "${HEALTH_TIMEOUT}" postgres',
    "compose run --rm --no-deps migrate",
    "python -m cloud_agent.identity_namespace --check",
    "compose ps --status running -q control-plane",
    "python -m cloud_agent.deploy_readiness",
    '--expected-deployment-sha "${SHA}"',
)


def test_the_control_plane_keeps_every_fence() -> None:
    for fence in FENCES:
        assert fence in CONTROL, f"{fence} went missing from the deploy script"


def test_there_is_still_exactly_one_migration_path() -> None:
    """A second one per mode is how two deployments drift apart."""
    assert CONTROL.count("compose run --rm --no-deps migrate") == 1


def test_source_mode_still_builds_what_it_fetched() -> None:
    assert "compose build" in CONTROL


def test_artifact_mode_cannot_build_an_image() -> None:
    """Both halves: no `build` subcommand, and no implicit pull at `up` either."""
    assert 'build) die "artifact deployments cannot build images"' in HOST
    assert "compose pull" in CONTROL, "artifact mode pulls, explicitly and before replacement"
    assert "--no-build --pull never" in CONTROL


def test_the_host_lets_go_of_the_environment_it_resolved_before_injecting() -> None:
    """Otherwise the stray-stack guard reads a world that no longer exists."""
    assert "_COMPOSE_ENVIRONMENT_LOADED=0" in HOST
    assert "load_compose_environment" in HOST


def test_the_stray_stack_guard_kept_its_meaning() -> None:
    """#175's property, which artifact mode must not quietly widen."""
    assert "SIBLING_COMPOSE_PROJECTS" in HOST
    assert "ALLOW_STRAY_STACK" not in HOST


def test_artifact_mode_requires_a_manifest_and_names_its_variant() -> None:
    assert "artifact mode requires AGENT_RELEASE_MANIFEST" in HELPER
    assert "AGENT_WEB_VARIANT" in HELPER
    assert "this recipe does not implement shared-live-exact-sha-v3" in HELPER, (
        "the recipe states the protocol it implements, so a mismatched pair refuses before "
        "anything is replaced"
    )


def test_only_release_owned_values_reach_the_command_line() -> None:
    """The resolved environment carries credentials; argv carries none of them.

    A key list rather than a filter: what may appear on a command line is a
    decision, and a value that arrives later cannot add itself to it.
    """
    assert "AGENT_DEPLOYMENT_SHA|AGENT_CONTROL_IMAGE" in HELPER
    assert "credentials, which must never become process arguments or logs" in HELPER
