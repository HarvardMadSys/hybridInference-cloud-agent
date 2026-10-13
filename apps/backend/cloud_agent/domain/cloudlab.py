"""Shared preference order for general-purpose CloudLab nodes."""

from typing import Final

# Verified against https://docs.cloudlab.us/hardware.html and
# https://www.cloudlab.us/portal-hardware.php on 2026-10-12. Each family was
# introduced in 2021 or later and has more than 20 total nodes. Prefer newer
# families first, then more CPU cores and RAM within the same introduction year.
# Keep this order independent of the changing number of currently free nodes.
DEFAULT_CLOUDLAB_NODE_TYPES: Final[tuple[str, ...]] = (
    "r6615",  # 2025; 80 nodes; 32 cores; 192 GiB RAM
    "c6620",  # 2025; 132 nodes; 28 cores; 128 GiB RAM
    "r650",  # 2022; 32 nodes; 72 cores; 256 GiB RAM
    "r6525",  # 2022; 32 nodes; 64 cores; 256 GiB RAM
    "c6525-100g",  # 2021; 36 nodes; 24 cores; 128 GiB RAM
    "c6525-25g",  # 2021; 144 nodes; 16 cores; 128 GiB RAM
)
