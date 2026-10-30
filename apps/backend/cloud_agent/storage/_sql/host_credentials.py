"""Host credential statements: authenticate, promote a renewed credential, issue one.

A host holds one credential, whose digest is ``credential_hash``. Renewal adds
two short-lived companions on the same row, never another host's:

* ``pending_credential_hash``: a renewed credential the control plane issued
  and the host has not presented yet. Presenting it promotes it, in the one
  statement that authenticates the request.
* ``previous_credential_hash``: the credential the promotion replaced, still
  accepted until ``previous_credential_expires_at`` so a second runner on the
  same machine (sharing the credential file) and requests already in flight
  are not refused.

Re-enrollment clears both, so a re-enrolled host's earlier credentials stop
at once, as before.
"""

from __future__ import annotations

#: How long a replaced credential keeps authenticating after its successor's first use.
PREVIOUS_CREDENTIAL_GRACE = "10 minutes"

PROMOTE_PENDING = """
UPDATE agent_runner_hosts
SET previous_credential_hash = credential_hash,
    previous_credential_expires_at = NOW() + INTERVAL '{grace}',
    credential_hash = pending_credential_hash,
    credential_key_id = pending_credential_key_id,
    pending_credential_hash = NULL,
    pending_credential_key_id = NULL,
    credential_renewal_requested = FALSE
WHERE host = $1 AND pending_credential_hash = $2
  AND credential_hash IS NOT NULL AND status <> 'disabled'
RETURNING {columns}, credential_hash AS current_credential_hash
"""

AUTHENTICATE = """
SELECT {columns}, credential_hash AS current_credential_hash FROM agent_runner_hosts
WHERE host = $1 AND credential_hash IS NOT NULL AND status <> 'disabled'
  AND (credential_hash = $2
       OR (previous_credential_hash = $2 AND previous_credential_expires_at > NOW()))
"""

#: Columns a re-enrollment resets, as an ``ON CONFLICT ... SET`` fragment.
RESET_ON_ENROLL = (
    "pending_credential_hash = NULL, pending_credential_key_id = NULL, "
    "previous_credential_hash = NULL, previous_credential_expires_at = NULL, "
    "credential_renewal_requested = FALSE, credential_key_id = NULL,"
)

__all__ = ["AUTHENTICATE", "PREVIOUS_CREDENTIAL_GRACE", "PROMOTE_PENDING", "RESET_ON_ENROLL"]
