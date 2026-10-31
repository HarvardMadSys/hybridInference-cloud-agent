"""Recognizing a pull request summary line that names no change.

A published session's draft pull request is titled from its first prompt and
changed files (``publish_worker._pr_title``); a summary line that only says
the work is done is skipped when one is chosen.
"""

from __future__ import annotations

import re

_GENERIC_SUMMARY = re.compile(
    r"(?:"
    r"(?:i|we)(?:['\u2019]ve| have)? (?:successfully )?(?:implemented|completed|finished|done) "
    r"(?:all |the |your |this |requested )*(?:feature|implementation|work|task|changes|request)"
    r"|(?:the )?(?:feature|implementation|work|task) "
    r"(?:is |has been )?(?:complete|completed|implemented|done)"
    r"|(?:here['\u2019]s|here is|here are) (?:a |the )?(?:brief |quick )?"
    r"(?:summary|overview|list of changes)(?: of (?:what (?:i|we) did|the changes))?"
    r"|summary|changes|what changed|done|completed"
    r")",
    re.IGNORECASE,
)


def is_generic_pr_summary(value: str) -> bool:
    """Recognize completion preambles that do not name the changed behavior."""
    opening = (
        re.split(r"[.!?]+(?=\s|$)|[\u3002\uff01\uff1f]", value, maxsplit=1)[0].strip().rstrip(" :")
    )
    return _GENERIC_SUMMARY.fullmatch(opening) is not None
