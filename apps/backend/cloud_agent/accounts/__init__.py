"""Cloud Agent's own accounts: email/password sign-in, sessions and the waiting list.

Off unless a deployment sets ``AGENT_AUTH_MODE=local``. Gateway mode, the
default, never reads these tables or routes.

The password, session-lifecycle, mail and abuse-control behaviour is adapted
from HybridInference's sign-in (``HarvardMadSys/hybridInference`` at
``321e9a6c457dcc80d35977aaac472d54041204a8``, MIT License, Copyright (c) 2026
Harvard SEAS — the same license and holder as this repository). Nothing here
imports, shares or reads that service's code, database, sessions or secrets.
See ``docs/developer/local-accounts.md``.
"""
