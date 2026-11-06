"""A host's member loopback service, as a fake sandbox manager finds it: already installed.

A direct plan's CLI starts behind its member's request meter, which the host
installs once per container (``compat_helper.install_compat_helper``). A launch
test about something else gives its fake manager this identity gate
(``manager.exact``), so the launch finds the helper in place and spends no exec
on it.
"""

from __future__ import annotations

from types import SimpleNamespace

HELPER_IN_PLACE = SimpleNamespace(
    proven_container=lambda spec: None,
    holds_helper=lambda proven, key: True,
    note_helper=lambda proven, key: None,
)
