"""Source-control names the wire models validate: repositories and git refs.

Kept apart from the GitHub client and the entitlement checks so that a schema
validating a request does not import either of them.
"""

from __future__ import annotations

# `owner/name`, the only shape GitHub uses and the only one the runner knows
# how to handle. Also keeps a leading `-` out of git's argv.
REPO_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}$"


# Characters `git check-ref-format` refuses anywhere in a ref name.
_REF_FORBIDDEN_CHARS = frozenset(" ~^:?*[\\")


def is_ref_name(ref: str) -> bool:
    """Whether git would accept ``ref`` as the name of a branch or tag.

    The rules of ``git check-ref-format --allow-onelevel``, plus no leading
    ``-``, which git refuses for a branch. A user's ref is sent to GitHub inside
    an API path, and these rules are also what keep it a ref there: no ``..``
    or dot-leading segment for the HTTP client to collapse into another
    endpoint, no ``?`` to start a query string, no control characters.
    """
    if not ref or ref == "@" or ref.startswith(("-", "/")) or ref.endswith(("/", ".")):
        return False
    if ".." in ref or "//" in ref or "@{" in ref:
        return False
    if any(char in _REF_FORBIDDEN_CHARS or ord(char) < 0x20 or ord(char) == 0x7F for char in ref):
        return False
    return not any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
