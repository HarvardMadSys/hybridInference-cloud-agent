"""Artifact deployment safeguards that complement runnable deployment tests.

The script checks below cover explicit build refusal, resolved-environment
refresh, required manifest/protocol wiring, and the credential-free argument
allowlist. Deployment fences and stray-stack refusal are exercised through the
real scripts in test_artifact_deploy.py and test_control_plane_deploy.py.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTROL = (REPO_ROOT / "deploy/control-plane/deploy.sh").read_text()
HOST = (REPO_ROOT / "deploy/host/sandbox_host.sh").read_text()
HELPER = (REPO_ROOT / "deploy/release/artifact_env.sh").read_text()


def test_artifact_mode_cannot_build_an_image() -> None:
    """Both halves: no `build` subcommand, and no implicit pull at `up` either."""
    assert 'build) die "artifact deployments cannot build images"' in HOST
    assert "compose pull" in CONTROL, "artifact mode pulls, explicitly and before replacement"
    assert "--no-build --pull never" in CONTROL


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
