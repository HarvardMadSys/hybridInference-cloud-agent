"""ThreadCheckpoint handling: encryption at rest and the claim-time gate.

The platform half of the COLD loop (design §6, issue #56). A checkpoint is the
runner-built envelope around one turn's exported harness state:

    {"harness_state": {"driver", "driver_version", "state_format_version",
                       "runtime_image_digest", "sha256", "size", "blob"},
     "consistency":   {"through_turn_id", "attempt_id"},
     "session_id":    "..."}

Two platform duties live here:

- **Encryption at rest.** Session files carry source, tool output and
  whatever secrets a repository leaked into either. Same server key as every
  other stored credential, different domain — a ciphertext from one table can
  never be decrypted, or planted, as another's.
- **The claim gate.** A checkpoint is handed to a runner only when it
  describes the thread's present: exported by the turn the follow-up
  continues from (``through_turn_id``), for the harness the job runs, at the
  CLI version the claiming host is about to run. Any mismatch answers "no
  checkpoint", and the runner replays — never old memory under new code.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from cloud_agent.source_control import SourceControlCipher, SourceControlError

logger = logging.getLogger(__name__)

CHECKPOINT_KIND = "checkpoint"
CHECKPOINT_DOMAIN = b"hybridinference/thread-checkpoint/v1"

#: Cap on the stored (encrypted) content. Postgres TOAST takes it, but WAL and
#: backups pay for every byte; the design starts conservative and telemetry
#: decides whether it moves.
MAX_CHECKPOINT_CONTENT_BYTES = 16 * 1024 * 1024

_cipher: SourceControlCipher | None = None
_cipher_missing_logged = False


def checkpoint_cipher() -> SourceControlCipher | None:
    """The checkpoint cipher, or ``None`` when no server key is configured.

    ``None`` disables the feature fail-closed: nothing is stored in the clear,
    nothing undecryptable is handed out, and every turn falls back to replay —
    which is always available and merely forgetful.
    """
    global _cipher, _cipher_missing_logged
    if _cipher is None:
        try:
            _cipher = SourceControlCipher(domain=CHECKPOINT_DOMAIN)
        except SourceControlError:
            if not _cipher_missing_logged:
                _cipher_missing_logged = True
                logger.warning(
                    "agent_checkpoint_cipher_unavailable",
                    extra={
                        "event": "agent_checkpoint_cipher_unavailable",
                        "detail": (
                            "no encryption key configured; thread checkpoints are "
                            "discarded and every cold turn replays"
                        ),
                    },
                )
            return None
    return _cipher


def open_checkpoint(ciphertext: str) -> dict[str, Any] | None:
    """Decrypt and parse a stored checkpoint; ``None`` on any defect."""
    cipher = checkpoint_cipher()
    if cipher is None:
        return None
    try:
        envelope = json.loads(cipher.decrypt(ciphertext))
    except (SourceControlError, json.JSONDecodeError):
        return None
    return envelope if isinstance(envelope, dict) else None


def gate_for_claim(
    envelope: dict[str, Any],
    *,
    runtime: str,
    parent_job_id: str,
    stamped_runtime_version: str | None,
) -> dict[str, Any] | None:
    """Return the envelope when it describes exactly what is about to run.

    ``stamped_runtime_version`` is the value the claim transaction just wrote
    onto the attempt — **never** a fresh read of the host row, which a rolling
    upgrade's heartbeat can move between claim and gate (the TOCTOU the
    stamping exists to kill; review finding 3).

    Every refusal is a silent downgrade to replay, which is the correct
    degradation (design §9) — but each is logged so "why did my thread
    forget" is answerable from the control plane's logs.
    """
    harness = envelope.get("harness_state") or {}
    consistency = envelope.get("consistency") or {}
    reason = None
    if harness.get("driver") != runtime:
        reason = "driver mismatch"
    elif consistency.get("through_turn_id") != parent_job_id:
        # The turn that exported this is not the turn being continued: an
        # export failed since, or retention misfired. Old memory + new code is
        # the one combination the design forbids outright.
        reason = "stale through_turn_id"
    else:
        exported_version = str(harness.get("driver_version") or "")
        stamped = str(stamped_runtime_version or "")
        if not exported_version or not stamped or exported_version != stamped:
            reason = (
                f"version mismatch (exported {exported_version or 'unknown'!r}, "
                f"attempt stamped {stamped or 'unknown'!r})"
            )
    if reason is not None:
        logger.info(
            "agent_checkpoint_withheld",
            extra={
                "event": "agent_checkpoint_withheld",
                "parent_job_id": parent_job_id,
                "detail": reason,
            },
        )
        return None
    return envelope
