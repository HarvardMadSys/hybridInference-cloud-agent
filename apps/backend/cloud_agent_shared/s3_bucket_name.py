"""What S3 accepts as the name of a general purpose bucket.

Both archive buckets are held to it: the deletion archive's, a control-plane
setting, and the lifecycle archive's, part of the policy every host's sandbox
manager applies. Each caller says in its own words what a refused name means.
"""

from __future__ import annotations

import re


def is_s3_bucket_name(value: str) -> bool:
    """Whether S3 accepts ``value`` as the name of a general purpose bucket."""
    return (
        bool(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value))
        and not re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", value)
        and not any(part in value for part in ("..", ".-", "-."))
        # Prefixes and suffixes S3 reserves for names it gives out itself.
        and not value.startswith(("xn--", "sthree-", "amzn-s3-demo-"))
        and not value.endswith(("-s3alias", "--ol-s3", ".mrap", "--x-s3", "--table-s3"))
    )


__all__ = ["is_s3_bucket_name"]
