"""Cloud Agent's accounts: sign-up, sign-in, sessions, the waiting list and administration.

They are this service's only identity source.

The password, session-lifecycle, mail and abuse-control behaviour is adapted
from HybridInference's sign-in (``HarvardMadSys/hybridInference`` at
``321e9a6c457dcc80d35977aaac472d54041204a8``, MIT License, Copyright (c) 2026
Harvard SEAS — the same license and holder as this repository). Nothing here
imports, shares or reads that service's code, database, sessions or secrets.
See ``docs/developer/local-accounts.md``.
"""
