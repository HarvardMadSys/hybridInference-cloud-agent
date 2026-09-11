"""Row decoding shared by the store and the admin read models."""

from __future__ import annotations

import json
from typing import Any

from cloud_agent.logging_utils import get_logger

logger = get_logger(__name__)


def load_json(value: Any) -> Any:
    """Decode a JSONB column that asyncpg may hand back as str or object."""
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to decode agent job JSON column", exc_info=True)
            return None
    return value
