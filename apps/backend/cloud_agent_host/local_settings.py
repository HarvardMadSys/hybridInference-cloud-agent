"""This host's own value for a runtime setting, before an admin value arrives."""

from __future__ import annotations

import os
import sys

from cloud_agent_shared.host_settings import HOST_SETTINGS_BY_KEY


def local_shared_resident_capacity() -> int:
    """This host's own sessions-per-sandbox value, or the default for one it cannot use.

    Only a bootstrap value: an admin's value replaces it on the first answer.
    An unusable local value therefore falls back to the default rather than
    keeping this host from advertising shared-live sandboxes at all.
    """
    setting = HOST_SETTINGS_BY_KEY["shared_resident_capacity"]
    raw = (os.environ.get("AGENT_SHARED_RESIDENT_CAPACITY") or "").strip()
    if not raw:
        return setting.default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if not setting.accepts(value):
        print(
            f"ignoring {setting.env}={raw!r}: not a whole number from "
            f"{setting.minimum} through {setting.maximum}",
            file=sys.stderr,
        )
        return setting.default
    return value
