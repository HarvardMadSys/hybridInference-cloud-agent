"""Artifact mode may not cost a fence, and may not gain a build.

Both stacks now have a second way to be deployed, and the failure worth a test
of its own is the quiet one: a mode that works while skipping the readiness
command, or that can build an image the release never pinned. Every string below
predates the split, and each is asserted by name so that making one conditional
is an argument somebody has to make rather than a line that slipped into a
branch.

This is the half that survived consolidation from the parallel implementation:
the two branches agreed on the mechanism and differed on what was pinned, and
this is what the other branch pinned.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL = (REPO_ROOT / "deploy/control-plane/deploy.sh").read_text()
HOST = (REPO_ROOT / "deploy/host/sandbox_host.sh").read_text()
HELPER = (REPO_ROOT / "deploy/release/artifact_env.sh").read_text()
HOST_WRAPPER = (REPO_ROOT / "deploy/host/deploy.sh").read_text()

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


def test_the_only_mode_branch_is_the_artifact_one() -> None:
    """The shape that makes every fence above mode-independent.

    Review mutated the two files in memory — readiness made source-only, and the
    host's cache reset deleted — and all eight string assertions still passed,
    because they asked whether a line exists rather than where it runs. This is
    the invariant that answers the real question: the default path is
    *unconditional*. Every mode-specific branch compares against `artifacts`, so
    a fence that was made conditional on `source` would have to introduce a
    comparison that does not otherwise exist anywhere in these scripts.

    It is also why the eight strings are worth asserting at all: with this shape,
    "the line is there" and "the line runs in every mode" are the same claim.
    """
    for name, script in (
        ("deploy/control-plane/deploy.sh", CONTROL),
        ("deploy/host/deploy.sh", HOST_WRAPPER),
        ("deploy/host/sandbox_host.sh", HOST),
    ):
        assert 'AGENT_DEPLOY_MODE" == "source' not in script, (
            f"{name} branches on source mode, which makes the default path conditional — and "
            "a deploy path that can differ per mode is one nobody has tested end to end"
        )
        assert 'AGENT_DEPLOY_MODE" == "artifacts' in script, (
            f"{name} declares a mode it never branches on"
        )


def test_the_host_drops_its_resolved_environment_after_the_release_replaces_it() -> None:
    """Order, not existence: the reset has to be the response to an injection.

    Review's second mutation — deleting the reset — passed the original
    assertion because `_COMPOSE_ENVIRONMENT_LOADED=0` also appears at
    initialisation. What matters is that it happens *after*
    `prepare_artifact_deployment` and *before* the next read, or the stray-stack
    guard answers from a world that no longer exists.
    """
    prepared = HOST.index("prepare_artifact_deployment host")
    reset = HOST.index("_COMPOSE_ENVIRONMENT_LOADED=0", prepared)
    reread = HOST.index("load_compose_environment", reset)
    assert prepared < reset < reread, (
        "the host has to re-read its environment between injecting the release values and "
        "asking the guard a question"
    )
    # The initialisation that looks like this at the top of the script is why an
    # existence check proved nothing: what matters is that a *second* one
    # follows the injection, and that the next read comes after it.


def test_artifact_mode_does_not_offer_the_stray_stack_bypass() -> None:
    """Source mode has one; this mode refuses instead of inviting an exception.

    `ALLOW_STRAY_STACK=1` is a deliberate escape hatch for an operator who knows
    what the second stack is. Adding the bypass to artifact mode as well would
    turn "the release and this host agree" into "whoever ran the deploy said
    they did" — the guard is the only thing standing between a shared machine
    and two stacks sharing enrollment credentials and tunnel aliases.
    """
    guard = HOST_WRAPPER[HOST_WRAPPER.index('if [ -n "${STRAY}" ]') :][:1500]
    artifact, _, source = guard.partition("ALLOW_STRAY_STACK")
    assert "artifacts" in artifact, "the artifact branch refuses before the bypass is read"
    assert "die" in artifact
    assert "ALLOW_STRAY_STACK" in source, (
        "and the hatch stays available to source mode, whose operator is looking at the "
        "machine rather than at a release"
    )


def test_artifact_mode_requires_a_manifest_and_names_its_variant() -> None:
    assert "artifact mode requires AGENT_RELEASE_MANIFEST" in HELPER
    assert "AGENT_WEB_VARIANT" in HELPER
    assert "this recipe does not implement shared-live-exact-sha-v4" in HELPER, (
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
