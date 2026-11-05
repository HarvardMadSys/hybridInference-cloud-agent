"""Shared bounds and constrained types for untrusted host reports."""

from __future__ import annotations

from typing import Annotated

from pydantic import BeforeValidator, Field

from cloud_agent_shared.shared_protocol import HOST_VERSION_MENU_MAX_ITEMS
from cloud_agent_shared.storable_text import storable_text

# What a host may say about itself, bounded. A host is trusted to run turns,
# not to size rows: these values are stored as JSONB that the admin pool view
# and the owner's job view read back on every load. Each cap sits far above
# anything a real runner sends — runtime and helper ids and their versions come
# from image labels, capabilities from the shared protocol's own constants, the
# worker id from a hostname — so only a machine that is misbehaving meets one.
# The version menu is the exception: versions an admin updated to are added to
# it on the host, so the host itself keeps it under the shared cap.
HOST_REPORT_KEY_MAX_CHARS = 64
HOST_REPORT_VERSION_MAX_CHARS = 128
HOST_CAPABILITY_MAX_CHARS = 256
HOST_LABELS_MAX_ITEMS = 32
HOST_LABEL_VALUE_MAX_CHARS = 255
WORKER_ID_MAX_CHARS = 512
_HostReportKey = Annotated[str, Field(max_length=HOST_REPORT_KEY_MAX_CHARS)]
_HostReportVersion = Annotated[str, Field(max_length=HOST_REPORT_VERSION_MAX_CHARS)]
_HostVersionMenu = Annotated[
    list[_HostReportVersion], Field(max_length=HOST_VERSION_MENU_MAX_ITEMS)
]
_HostCapability = Annotated[str, Field(max_length=HOST_CAPABILITY_MAX_CHARS)]
_HostLabelValue = Annotated[str, Field(max_length=HOST_LABEL_VALUE_MAX_CHARS)]


def _storable(value: object) -> object:
    return storable_text(value) if isinstance(value, str) else value


#: A reason a host reports in its sandbox's own words -- a CLI's last error, the
#: tail of what it printed. Text Postgres cannot store is replaced, not refused:
#: refusing the reason would refuse the report that carries it. The replacement
#: comes before the field's own checks, which cannot measure a surrogate.
ReportedText = Annotated[str, BeforeValidator(_storable)]
