"""Static deployment setup guidance shown beside the editable settings.

These entries name deployment-managed configuration and where an administrator
can manage or read about it. They are not settings: nothing here is stored,
validated or changed by this module. A state describes only facts this process
already has; it never reads a mounted file, loads a compute profile or parses a
pgAdmin address, and it exposes neither values nor paths.

The ``state`` vocabulary distinguishes "configured" from "healthy", and an
unused optional feature (``not_used``) from a missing required one.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Literal

from cloud_agent.setting_definition import MINIMUM_CREDENTIAL_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

DeploymentState = Literal["configured", "missing", "weak", "fallback", "not_used", "unknown"]

DEPLOYMENT_STATES = frozenset({"configured", "missing", "weak", "fallback", "not_used", "unknown"})

#: The application-owned navigation targets that may appear in an action.
#: The frontend resolves these fixed names; it never follows a backend URL.
KNOWN_DEPLOYMENT_SETUP_TARGETS = frozenset(
    {
        "github_app_private_key",
        "compute_setup",
        "database",
        "pgadmin_url",
        "deployment_guide",
        "key_rotation_guide",
    }
)

_DEDICATED_ROOT_ENV = "AGENT_SETTINGS_ENCRYPTION_KEY"
_FALLBACK_ROOT_ENV = "AGENT_SESSION_SECRET"
_GITHUB_KEY_PATH_ENV = "AGENT_GITHUB_APP_PRIVATE_KEY_PATH"
_COMPUTE_CONFIG_ENV = "AGENT_COMPUTE_CONFIG"
_PGADMIN_BOOTSTRAP_ENVS = (
    "AGENT_PGADMIN_BOOTSTRAP_URL",
    "AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL",
    "AGENT_PGADMIN_BOOTSTRAP_DIR",
)

BOOTSTRAP: tuple[dict[str, Any], ...] = (
    {
        "id": "database",
        "key": "AGENT_DATABASE_URL",
        "label": "Database connection",
        "description": (
            "Required before stored settings can be read. This process reading the "
            "settings snapshot does not describe replica, backup or cluster health. "
            "Connection changes, migration and rollback are controlled deployment "
            "operations; see the deployment guide."
        ),
        "management": "deployment",
        "requirement": "required",
        "actions": [{"target": "deployment_guide", "label": "View deployment guide"}],
    },
    {
        "id": "settings_root_key",
        "key": "AGENT_SETTINGS_ENCRYPTION_KEY / AGENT_SESSION_SECRET",
        "label": "Settings encryption key",
        "description": (
            "Use a stable dedicated encryption key of at least 32 bytes. Without one, "
            "the deployment environment's AGENT_SESSION_SECRET is the fallback; that "
            "fallback is separate from the saved session signing secret. When the root "
            "changes, only data encrypted under that root needs re-encryption. Moving "
            "from the fallback to a dedicated key with exactly the same bytes keeps "
            "existing ciphertext readable; setting a new dedicated value is a rotation "
            "and requires re-encryption."
        ),
        "management": "rotation",
        "requirement": "required",
        "actions": [{"target": "key_rotation_guide", "label": "Read key management notes"}],
    },
    {
        "id": "recovery_token",
        "key": "ADMIN_TOKEN",
        "label": "Administrator recovery access",
        "description": (
            "Optional deployment credential for the setup page when Gateway sign-in is "
            "not configured. It remains independent of saved settings; changing it does "
            "not repair an unavailable database or unreadable encryption key."
        ),
        "management": "deployment",
        "requirement": "optional",
        "actions": [{"target": "deployment_guide", "label": "View deployment guide"}],
    },
    {
        "id": "github_app_key_file",
        "key": "AGENT_GITHUB_APP_PRIVATE_KEY_PATH",
        "label": "Mounted GitHub App key",
        "description": (
            "Deployment file path for the GitHub App private key. Alternatively, save "
            "the inline private key in GitHub settings; the mounted file is not needed "
            "when that saved key is configured."
        ),
        "management": "settings",
        "requirement": "conditional",
        "actions": [{"target": "github_app_private_key", "label": "Edit GitHub App private key"}],
    },
    {
        "id": "compute_config_file",
        "key": "AGENT_COMPUTE_CONFIG",
        "label": "Compute deployment profiles",
        "description": (
            "Profiles come from either an administrator-registered profile in Compute "
            "setup or a deployment file profile. The deployment value stays operator "
            "configuration; use Compute setup for registered profiles."
        ),
        "management": "specialized",
        "requirement": "conditional",
        "actions": [{"target": "compute_setup", "label": "Open Compute setup"}],
    },
    {
        "id": "pgadmin_bootstrap",
        "key": (
            "AGENT_PGADMIN_BOOTSTRAP_URL / AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL / "
            "AGENT_PGADMIN_BOOTSTRAP_DIR"
        ),
        "label": "pgAdmin first-time setup",
        "description": (
            "Deployment routing and the private setup volume let the first administrator "
            "create the account in Admin Database. Setup saves the connection; later URL "
            "changes use the Database tools settings, and other services remain limited "
            "by the configured allowlist."
        ),
        "management": "specialized",
        "requirement": "optional",
        "actions": [
            {"target": "pgadmin_url", "label": "Edit pgAdmin URL"},
            {"target": "database", "label": "Open Admin Database"},
        ],
    },
    {
        "id": "infrastructure",
        "key": "Host networking, isolation, listener and build configuration",
        "label": "Deployment infrastructure",
        "description": (
            "Listener addresses, mounts, networks, host isolation, egress and build "
            "routing are deployment configuration. Application policies such as proxy "
            "trust are saved settings where the service exposes them; not every such "
            "policy is deployment-only."
        ),
        "management": "deployment",
        "requirement": "conditional",
        "actions": [{"target": "deployment_guide", "label": "View deployment guide"}],
    },
)


def _long_enough(value: str) -> bool:
    """Whether a credential reaches the registry's UTF-8 byte floor."""
    return len(value.encode("utf-8")) >= MINIMUM_CREDENTIAL_BYTES


def _root_key_state(_values: Mapping[str, Any]) -> DeploymentState:
    """Report the key the settings cipher resolves, never its value.

    A dedicated key wins whenever it is set, so a short dedicated value is
    ``missing`` even when the fallback is long: it is the key in use.
    """
    dedicated = os.environ.get(_DEDICATED_ROOT_ENV) or ""
    fallback = os.environ.get(_FALLBACK_ROOT_ENV) or ""
    if dedicated:
        return "configured" if _long_enough(dedicated) else "missing"
    if fallback:
        return "fallback" if _long_enough(fallback) else "missing"
    return "missing"


def _admin_token_state(_values: Mapping[str, Any]) -> DeploymentState:
    """Describe configuration only; a configured token proves no auth health."""
    value = (os.environ.get("ADMIN_TOKEN") or "").strip()
    if not value:
        return "not_used"
    return "configured" if _long_enough(value) else "weak"


def _github_key_state(values: Mapping[str, Any]) -> DeploymentState:
    """Prefer the saved inline key; a configured mount is not probed or read."""
    if values.get("github_app_private_key"):
        return "not_used"
    return "unknown" if os.environ.get(_GITHUB_KEY_PATH_ENV, "").strip() else "not_used"


def _compute_config_state(_values: Mapping[str, Any]) -> DeploymentState:
    """A named compute file is unverified here and is never read."""
    return "unknown" if os.environ.get(_COMPUTE_CONFIG_ENV, "").strip() else "not_used"


def _pgadmin_bootstrap_state(_values: Mapping[str, Any]) -> DeploymentState:
    """Any bootstrap variable is an unverified setup; none means unused.

    ``PUBLIC_URL`` has a valid default, so it is not required for this state.
    """
    configured = any((os.environ.get(name) or "").strip() for name in _PGADMIN_BOOTSTRAP_ENVS)
    return "unknown" if configured else "not_used"


def _database_state(_values: Mapping[str, Any]) -> DeploymentState:
    """The settings snapshot was read successfully for this response."""
    return "configured"


def _infrastructure_state(_values: Mapping[str, Any]) -> DeploymentState:
    """Infrastructure is deployment-managed and not inspectable from here."""
    return "unknown"


_STATE_RESOLVERS: dict[str, Callable[[Mapping[str, Any]], DeploymentState]] = {
    "database": _database_state,
    "settings_root_key": _root_key_state,
    "recovery_token": _admin_token_state,
    "github_app_key_file": _github_key_state,
    "compute_config_file": _compute_config_state,
    "pgadmin_bootstrap": _pgadmin_bootstrap_state,
    "infrastructure": _infrastructure_state,
}


def _safe_state(
    resolver: Callable[[Mapping[str, Any]], DeploymentState],
    values: Mapping[str, Any],
) -> DeploymentState:
    """A status probe is auxiliary: any failure degrades to ``unknown``."""
    try:
        return resolver(values)
    except Exception:
        return "unknown"


def bootstrap_snapshot(values: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return fresh deployment rows with optional states for one settings read.

    Every returned row and action list is newly allocated, so the caller or the
    JSON encoder cannot mutate :data:`BOOTSTRAP` for the next reader.
    """
    snapshot: list[dict[str, Any]] = []
    for row in BOOTSTRAP:
        copied: dict[str, Any] = dict(row)
        copied["actions"] = [dict(action) for action in row["actions"]]
        copied["state"] = _safe_state(_STATE_RESOLVERS[str(row["id"])], values)
        snapshot.append(copied)
    return snapshot
