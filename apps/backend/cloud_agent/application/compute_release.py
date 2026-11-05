"""Release a committed allocation's provider resource and keep the proof it ended on.

The proof is recorded on the operation as ``release_evidence``: where it came
from, when the provider was observed (or when an older terminal record was
saved), the HTTP status the provider actually returned, and the immutable
identity it concerns. Never a response body,
configuration, address, credential or provider label.

``delete_acknowledged``
    The provider answered this operation's DELETE of exactly this resource
    with its documented acknowledgement. Once saved durably, a retry of the
    same operation finishes on it instead of deleting again. A crash before
    that save still leaves an unknown outcome; no acknowledgement is invented.
``provider_terminal``
    The provider reported the resource terminated when read.
``expired_not_found``
    The provider no longer shows the resource and its own recorded lease
    has run out (``ComputeReconciler._expired_absence``).
``never_created``
    No provider resource was ever named; nothing was read or deleted.

A not-found or unknown answer is none of these and never completes a release.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from cloud_agent.compute_secrets import ComputeSecretError
from cloud_agent.ports.external.compute_provider import (
    ProviderError,
    ProviderReceipt,
    ResourceObservation,
    ResourceRef,
)

if TYPE_CHECKING:
    from cloud_agent.application.compute_reconcile import ComputeReconciler

#: The raw state of a resource taken as ended because its own lease ran out.
EXPIRED_NOT_FOUND = "expired_not_found"
NEVER_CREATED: dict[str, Any] = {"source": "never_created"}


def observed_evidence(source: str, observation: ResourceObservation) -> dict[str, Any]:
    """The safe facts of one provider observation."""
    evidence: dict[str, Any] = {
        "source": source,
        "external_id": observation.ref.external_id,
        "observed_at": observation.observed_at.isoformat(),
    }
    if observation.provider_http_status is not None:
        evidence["http_status"] = observation.provider_http_status
    return evidence


def recorded_receipt(operation: dict[str, Any], external_id: str) -> dict[str, Any] | None:
    """This operation's saved DELETE acknowledgement of exactly this resource."""
    evidence = (operation.get("result") or {}).get("release_evidence")
    if (
        isinstance(evidence, dict)
        and evidence.get("source") == "delete_acknowledged"
        and evidence.get("operation_id") == operation["id"]
        and evidence.get("external_id") == external_id
        and isinstance(evidence.get("observed_at"), str)
    ):
        return evidence
    return None


def acknowledged_release(operation: dict[str, Any], ref: ResourceRef) -> ResourceObservation | None:
    """The terminal observation a saved DELETE acknowledgement of ``ref`` stands for."""
    receipt = recorded_receipt(operation, ref.external_id)
    if receipt is None:
        return None
    return ResourceObservation(
        ref=ref,
        state="terminated",
        raw_state="delete_acknowledged",
        observed_at=datetime.fromisoformat(receipt["observed_at"]),
        provider_http_status=receipt.get("http_status"),
        lease_known=True,
        scope_complete=True,
    )


def release_evidence(operation: dict[str, Any], observation: ResourceObservation) -> dict[str, Any]:
    """What a terminal observation proves: this operation's DELETE answer, or the read."""
    receipt = recorded_receipt(operation, observation.ref.external_id)
    if receipt is not None:
        return receipt
    source = (
        "expired_not_found" if observation.raw_state == EXPIRED_NOT_FOUND else "provider_terminal"
    )
    return observed_evidence(source, observation)


def recorded_evidence(
    operation: dict[str, Any], allocation: dict[str, Any]
) -> dict[str, Any] | None:
    """What an allocation already recorded as terminal ended on, if it was the provider.

    Local inventory removal of an attached machine records absence with an
    identity but no provider evidence, so it gives none. The allocation timestamp
    is a database recording time, not a provider observation time.
    """
    external_id = allocation.get("external_id")
    if not external_id:
        return NEVER_CREATED if allocation.get("observed_state") == "absent" else None
    receipt = recorded_receipt(operation, external_id)
    if receipt is not None:
        return receipt
    provider_state = allocation.get("provider_state")
    if provider_state in {EXPIRED_NOT_FOUND, "delete_acknowledged"}:
        source = provider_state
    elif allocation.get("observed_state") == "terminated":
        source = "provider_terminal"
    else:
        return None
    observed_at = allocation.get("observed_at")
    evidence = {"source": source, "external_id": external_id}
    if isinstance(observed_at, datetime):
        evidence["recorded_at"] = observed_at.isoformat()
    return evidence


class ComputeRelease:
    """The provider half of a retirement, after its local release has committed."""

    def __init__(self, reconciler: ComputeReconciler) -> None:
        self.reconciler = reconciler

    async def released(
        self,
        operation: dict[str, Any],
        evidence: dict[str, Any] | None,
        message: str = "Provider confirmed that the resource is terminated.",
    ) -> None:
        """Finish the release on its proof."""
        result: dict[str, Any] = {"external_pending": False, "blocked_reasons": []}
        if evidence is not None:
            result["release_evidence"] = evidence
        await self.reconciler._finish(operation, "succeeded", "released", message, result=result)

    async def keep_receipt(
        self, operation: dict[str, Any], ref: ResourceRef, receipt: ProviderReceipt
    ) -> None:
        """Save a DELETE acknowledgement of ``ref`` before anything after it can fail."""
        observation = receipt.observation
        if (
            receipt.outcome != "confirmed"
            or observation is None
            or observation.state != "terminated"
            or observation.raw_state != "delete_acknowledged"
            or observation.ref.external_id != ref.external_id
        ):
            return
        evidence = {
            **observed_evidence("delete_acknowledged", observation),
            "operation_id": operation["id"],
        }
        await self.reconciler._checkpoint(operation, "releasing", {"release_evidence": evidence})

    async def committed(self, operation: dict[str, Any], allocation: dict[str, Any]) -> None:
        """Delete the committed allocation's resource, or finish on proof it is gone."""
        r = self.reconciler
        if allocation.get("observed_state") in {"terminated", "absent"}:
            # Terminal proof is final: a later read can only be stale.
            await self.released(operation, recorded_evidence(operation, allocation))
            return
        ref = r._ref(allocation)
        acknowledged = acknowledged_release(operation, ref)
        if acknowledged is not None:
            # This operation's DELETE was answered; it is not sent again.
            await r._save_observation(operation, allocation, acknowledged)
            await self.released(operation, release_evidence(operation, acknowledged))
            return
        provider = r.commands.registry.get(allocation["provider_id"]).provider
        # The durable commit already passed the inventory and fencing gates.
        # An extra read must not prevent the first DELETE of that exact identity.
        # Once submitted, however, its unknown outcome still requires observation
        # before replay; only a proven rejection clears this marker below.
        if operation.get("result", {}).get("release_requested"):
            observation = await r._inspect(operation, allocation)
            if observation.state == "terminated":
                await self.released(operation, release_evidence(operation, observation))
                return
            if observation.state != "ready":
                await r._finish(
                    operation,
                    "waiting",
                    "confirming_release",
                    "Release was submitted; checking provider state before declaring success.",
                    result={"external_pending": True},
                    retry=30,
                )
                return
        await r._checkpoint(
            operation, "releasing", {"release_requested": True, "external_pending": True}
        )
        try:
            receipt = await provider.release(ref, r._intent(operation, allocation))
        except (ProviderError, ComputeSecretError) as exc:
            if getattr(exc, "code", "credential_unavailable") != "result_unknown":
                await r._checkpoint(
                    operation,
                    "release_rejected",
                    {"release_requested": False, "external_pending": False},
                )
            raise
        await self.keep_receipt(operation, ref, receipt)
        observation = receipt.observation or await provider.inspect(ref)
        await r._save_observation(operation, allocation, observation)
        if observation.state == "terminated":
            await self.released(operation, release_evidence(operation, observation))
        else:
            await r._finish(
                operation,
                "waiting",
                "confirming_release",
                "Waiting for provider termination confirmation.",
                result={"external_pending": True},
                retry=30,
            )
