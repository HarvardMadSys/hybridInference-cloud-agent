"""The local branch a conversation's checkout is put on."""

from __future__ import annotations

import re


def branch_name_for(job_id: str) -> str:
    """Return the ``agent/<id>`` branch name for a conversation or job id.

    Dots are dropped rather than kept: git rejects refs containing ``..`` or
    ending in ``.``, so a naive sanitizer that preserves them can emit a name
    git refuses. Ids are generated (``athr_<hex>``), so this is defensive
    against a hand-crafted id rather than a routine transformation.
    """
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", job_id).strip("-") or "unnamed"
    return f"agent/{safe}"
