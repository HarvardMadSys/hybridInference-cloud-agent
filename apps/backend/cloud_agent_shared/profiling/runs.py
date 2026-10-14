"""Validate run descriptors and build their immutable catalogue manifests."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .clock import clock
from .models import Measurement, ProfilingRun
from .schema import _TRUTHY, MAX_RUN_ID_CHARS, PRODUCER_INSTANCE_ID, RUN_ID_PATTERN, TARGET_KEYS


def _truthy(raw: str | None) -> bool:
    """Return whether an environment value means "on"."""
    return (raw or "").strip().lower() in _TRUTHY


def _parse_absolute_expiry(raw: str) -> datetime:
    """Return the configured absolute expiry of a run, normalized to UTC.

    A trailing ``Z`` is accepted because the design's run descriptors are
    written in UTC, and any other ISO-8601 offset is normalized to UTC so two
    spellings of one instant compare equal.

    Raises:
        ValueError: the value is missing, unparseable, or not in the future.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("expires_at is required")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"expires_at is not an ISO-8601 timestamp: {text!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    moment = moment.astimezone(timezone.utc)
    if (moment - datetime.now(timezone.utc)).total_seconds() <= 0:
        raise ValueError(f"expires_at is not in the future: {text!r}")
    return moment


def _parse_expiry(raw: str) -> float:
    """Return the monotonic deadline for an ISO-8601 ``expires_at``.

    Derived from :func:`_parse_absolute_expiry`, so the configured instant is
    read in exactly one place.
    """
    moment = _parse_absolute_expiry(raw)
    return clock().monotonic() + (moment - datetime.now(timezone.utc)).total_seconds()


def parse_run(raw: str, *, allow_continuous: bool = False) -> ProfilingRun:
    """Build a run descriptor from its JSON form.

    Raises:
        ValueError: the descriptor is not an object, is missing a run id, a
            non-empty operation list, a future expiry or a positive cap, or
            scopes itself by a field that does not exist.
    """
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run descriptor is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("run descriptor must be a JSON object")

    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run descriptor has no run_id")
    if len(run_id) > MAX_RUN_ID_CHARS:
        raise ValueError(f"run_id is longer than {MAX_RUN_ID_CHARS} characters")
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("run_id must contain only letters, numbers, '.', '_', ':' or '-'")

    raw_operations = body.get("operations")
    if not isinstance(raw_operations, list):
        raise ValueError("run descriptor has no operations list")
    operations = frozenset(str(item).strip() for item in raw_operations if str(item).strip())
    if not operations:
        raise ValueError("run descriptor selects no operations")

    continuous = body.get("continuous") is True
    if continuous and (
        not allow_continuous or "expires_at" not in body or body["expires_at"] is not None
    ):
        raise ValueError("continuous runs require Admin control and an explicit null expires_at")
    if "continuous" in body and not isinstance(body["continuous"], bool):
        raise ValueError("continuous must be a boolean")

    try:
        if isinstance(body.get("max_operations"), bool):
            raise TypeError
        max_operations = int(body["max_operations"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run descriptor has no valid max_operations") from exc
    if (continuous and max_operations != 0) or (not continuous and max_operations <= 0):
        raise ValueError("max_operations must be zero for continuous runs, positive otherwise")

    try:
        if isinstance(body.get("config_version"), bool):
            raise TypeError
        config_version = int(body.get("config_version") or 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("config_version must be an integer") from exc
    if config_version <= 0:
        raise ValueError("config_version must be positive")

    raw_admissions = body.get("admission_instances") or []
    if not isinstance(raw_admissions, list):
        raise ValueError("admission_instances must be a list")
    admissions: list[Mapping[str, Any]] = []
    for raw_admission in raw_admissions:
        if not isinstance(raw_admission, Mapping):
            raise ValueError("admission_instances entries must be objects")
        instance = str(raw_admission.get("instance") or "").strip()
        if not instance:
            raise ValueError("admission_instances entries need an instance")
        raw_budget = raw_admission.get("max_operations")
        if (
            not isinstance(raw_budget, (str, int))
            or isinstance(raw_budget, bool)
            or not str(raw_budget).strip()
        ):
            raise ValueError("admission_instances entries need max_operations")
        try:
            budget = int(raw_budget)
        except ValueError as exc:
            raise ValueError("admission_instances entries need max_operations") from exc
        if budget < 0 or (budget == 0 and not continuous):
            raise ValueError("admission instance max_operations must be positive unless continuous")
        admissions.append({"instance": instance, "max_operations": budget})
    if not any(item["instance"] == PRODUCER_INSTANCE_ID for item in admissions):
        admissions.append({"instance": PRODUCER_INSTANCE_ID, "max_operations": max_operations})

    raw_target = body.get("target") or {}
    if not isinstance(raw_target, dict):
        raise ValueError("target must be a JSON object")
    unknown = sorted(str(key) for key in raw_target if str(key) not in TARGET_KEYS)
    if unknown:
        raise ValueError(f"target names unknown field(s): {', '.join(unknown)}")
    target: dict[str, str] = {}
    for key, value in raw_target.items():
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
            raise ValueError(f"target.{key} must be a non-empty string or integer")
        target[str(key)] = str(value)

    raw_expiry = str(body.get("expires_at") or "")
    return ProfilingRun(
        run_id=run_id,
        operations=operations,
        expires_at=raw_expiry.strip(),
        expires_at_utc=None if continuous else _parse_absolute_expiry(raw_expiry),
        deadline_mono=math.inf if continuous else _parse_expiry(raw_expiry),
        continuous=continuous,
        max_operations=max_operations,
        target=target,
        config_version=config_version,
        admission_instances=tuple(admissions),
    )


def manifest_for_run(run: ProfilingRun | None) -> dict[str, Any] | None:
    """Return the immutable catalogue manifest for one parsed run."""
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "operations": sorted(run.operations),
        "target": dict(run.target),
        "expires_at": run.expires_at_utc.isoformat() if run.expires_at_utc else None,
        **({"continuous": True} if run.continuous else {}),
        "config_version": run.config_version,
        "max_operations": run.max_operations,
        "admission_instances": [dict(item) for item in run.admission_instances],
    }


def manifest_for_measurement(measurement: Measurement | None) -> dict[str, Any] | None:
    """Return the run manifest this measurement was selected under.

    Built from the measurement's *own* copy of the experiment rather than from
    whatever run is configured now. That is what stops a delivery that happens
    after a new experiment started from describing the old record as a sample
    of the new one -- a mix-up the writer would reject as ``run_conflict`` and
    an operator would have to debug from two disagreeing manifests.
    """
    if measurement is None:
        return None
    if not measurement.run_id:
        return None
    manifest: dict[str, Any] = {
        "run_id": measurement.run_id,
        "operations": sorted(measurement.run_operations),
        "target": dict(measurement.run_target),
        "config_version": measurement.run_config_version,
        "max_operations": measurement.run_max_operations,
        "admission_instances": [dict(item) for item in measurement.run_admission_instances],
    }
    # The configured expiry travels with the record, so a measurement that was
    # handed over rather than selected here still states the experiment's
    # identity instead of deriving a different instant from its own clock.
    if measurement.run_expires_at is None and not measurement.run_continuous:
        return None
    manifest["expires_at"] = (
        measurement.run_expires_at.isoformat() if measurement.run_expires_at else None
    )
    if measurement.run_continuous:
        manifest["continuous"] = True
    return manifest
