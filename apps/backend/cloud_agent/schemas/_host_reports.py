"""Shared bounds and constrained types for untrusted host reports."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

# What a host may say about itself, bounded. A host is trusted to run turns,
# not to size rows: these values are stored as JSONB that the admin pool view
# and the owner's job view read back on every load. Each cap sits far above
# anything a real runner sends — runtime and helper ids and their versions come
# from image labels, capabilities from the shared protocol's own constants, the
# worker id from a hostname — so only a machine that is misbehaving meets one.
HOST_REPORT_KEY_MAX_CHARS = 64
HOST_REPORT_VERSION_MAX_CHARS = 128
HOST_CAPABILITY_MAX_CHARS = 256
HOST_VERSION_MENU_MAX_ITEMS = 64
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
