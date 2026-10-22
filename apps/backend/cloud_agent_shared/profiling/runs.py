"""Normalize run descriptors and project immutable catalogue manifests."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from .clock import clock
from .models import AdmissionInstance, Measurement, ProfilingRun, RunManifest
from .schema import (
    _TRUTHY,
    MAX_REMOTE_STAGES,
    MAX_RUN_ID_CHARS,
    PRODUCER_INSTANCE_ID,
    RUN_ID_PATTERN,
    SOURCE_ADMIN,
    SOURCE_ENVIRONMENT,
    TARGET_KEYS,
)


def _truthy(raw: str | None) -> bool:
    """Return whether an environment value means "on"."""
    return (raw or "").strip().lower() in _TRUTHY


def _parse_instant(raw: Any) -> datetime:
    """Return an ISO-8601 instant normalized to UTC, without judging its age."""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("expires_at is required")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"expires_at is not an ISO-8601 timestamp: {text!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def _future_expiry(raw: Any) -> tuple[datetime, float]:
    """Return one configured expiry and the process-local deadline derived from it."""
    moment = _parse_instant(raw)
    remaining = (moment - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        raise ValueError(f"expires_at is not in the future: {str(raw).strip()!r}")
    return moment, clock().monotonic() + remaining


def _run_id(body: Mapping[str, Any]) -> str:
    """Normalize and validate a run id."""
    run_id = str(body.get("run_id") or "").strip()
    if not run_id:
        raise ValueError("run descriptor has no run_id")
    if len(run_id) > MAX_RUN_ID_CHARS:
        raise ValueError(f"run_id is longer than {MAX_RUN_ID_CHARS} characters")
    if RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("run_id must contain only letters, numbers, '.', '_', ':' or '-'")
    return run_id


def _operations(
    body: Mapping[str, Any], *, accepted: tuple[type[Any], ...] = (list,)
) -> frozenset[str]:
    """Normalize the non-empty operation selection."""
    raw = body.get("operations")
    if not isinstance(raw, accepted):
        raise ValueError("run descriptor has no operations list")
    operations = frozenset(str(item).strip() for item in raw if str(item).strip())
    if not operations:
        raise ValueError("run descriptor selects no operations")
    return operations


def _continuous(body: Mapping[str, Any], *, allow: bool, source: str) -> bool:
    """Validate the explicit continuous-run contract."""
    if "continuous" in body and not isinstance(body["continuous"], bool):
        raise ValueError("continuous must be a boolean")
    continuous = body.get("continuous") is True
    if continuous and (
        not allow
        or source != SOURCE_ADMIN
        or "expires_at" not in body
        or body["expires_at"] is not None
    ):
        raise ValueError("continuous runs require Admin control and an explicit null expires_at")
    return continuous


def _max_operations(body: Mapping[str, Any], *, continuous: bool) -> int:
    """Normalize the finite or continuous admission budget."""
    try:
        raw = body["max_operations"]
        if isinstance(raw, bool):
            raise TypeError
        value = int(raw)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run descriptor has no valid max_operations") from exc
    if (continuous and value != 0) or (not continuous and value <= 0):
        raise ValueError("max_operations must be zero for continuous runs, positive otherwise")
    return value


def _config_version(body: Mapping[str, Any]) -> int:
    """Normalize a positive manifest version without defaulting explicit zero."""
    raw = body.get("config_version", 1)
    try:
        if isinstance(raw, bool):
            raise TypeError
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("config_version must be an integer") from exc
    if value <= 0:
        raise ValueError("config_version must be positive")
    return value


def _target(body: Mapping[str, Any]) -> dict[str, str]:
    """Normalize the bounded target vocabulary."""
    raw = body.get("target") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("target must be a JSON object")
    unknown = sorted(str(key) for key in raw if str(key) not in TARGET_KEYS)
    if unknown:
        raise ValueError(f"target names unknown field(s): {', '.join(unknown)}")
    normalized: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(value, (str, int)) or isinstance(value, bool) or not str(value).strip():
            raise ValueError(f"target.{key} must be a non-empty string or integer")
        normalized[str(key)] = str(value)
    return normalized


def _admissions(
    raw: Any,
    *,
    continuous: bool,
    bounded: bool,
) -> tuple[AdmissionInstance, ...]:
    """Normalize immutable admission entries under one boundary's policy."""
    if raw is None:
        values: Sequence[Any] = ()
    elif isinstance(raw, list):
        values = raw
    else:
        raise ValueError("admission_instances must be a list")
    if bounded and len(values) > MAX_REMOTE_STAGES:
        raise ValueError("admission_instances has too many entries")
    admissions: list[AdmissionInstance] = []
    for item in values:
        if not isinstance(item, Mapping):
            raise ValueError("admission_instances entries must be objects")
        instance = str(item.get("instance") or "").strip()
        if not instance:
            raise ValueError("admission_instances entries need an instance")
        raw_budget = item.get("max_operations")
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
        admissions.append(AdmissionInstance(instance=instance, max_operations=budget))
    return tuple(admissions)


def _configured_admissions(
    raw: Any, *, continuous: bool, max_operations: int
) -> tuple[AdmissionInstance, ...]:
    """Normalize descriptor admissions and add this producer once."""
    admissions = list(_admissions(raw, continuous=continuous, bounded=False))
    if not any(item.instance == PRODUCER_INSTANCE_ID for item in admissions):
        admissions.append(
            AdmissionInstance(
                instance=PRODUCER_INSTANCE_ID,
                max_operations=max_operations,
            )
        )
    return tuple(admissions)


def parse_run(
    raw: str,
    *,
    allow_continuous: bool = False,
    source: str = SOURCE_ENVIRONMENT,
) -> ProfilingRun:
    """Build a fail-fast run descriptor from deployment or control configuration.

    Raises:
        ValueError: the descriptor is malformed, out of scope, expired, or
            internally inconsistent.
    """
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"run descriptor is not valid JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise ValueError("run descriptor must be a JSON object")

    continuous = _continuous(body, allow=allow_continuous, source=source)
    max_operations = _max_operations(body, continuous=continuous)
    expires_at: datetime | None
    if continuous:
        expires_at, deadline = None, math.inf
    else:
        expires_at, deadline = _future_expiry(body.get("expires_at"))
    manifest = RunManifest(
        run_id=_run_id(body),
        operations=_operations(body),
        target=_target(body),
        expires_at=expires_at,
        continuous=continuous,
        config_version=_config_version(body),
        max_operations=max_operations,
        admission_instances=_configured_admissions(
            body.get("admission_instances"),
            continuous=continuous,
            max_operations=max_operations,
        ),
        source=source,
    )
    return ProfilingRun(manifest=manifest, deadline_mono=deadline)


def inherited_manifest(context: Mapping[str, Any]) -> RunManifest | None:
    """Fail softly while normalizing the manifest carried by a selection.

    Syntax is validated here; whether a finite expiry is still in the future is
    deliberately left to the local deadline adapter. A stale handoff is normal
    transport input, not malformed configuration.
    """
    raw = context.get("run")
    if not isinstance(raw, Mapping):
        return None
    try:
        source = str(raw.get("source") or SOURCE_ADMIN)
        continuous = _continuous(raw, allow=True, source=source)
        max_operations = _max_operations(raw, continuous=continuous)
        expires_at = None if continuous else _parse_instant(raw.get("expires_at"))
        return RunManifest(
            run_id=_run_id(raw),
            operations=_operations(
                raw,
                accepted=(list, tuple, set, frozenset),
            ),
            target=_target(raw),
            expires_at=expires_at,
            max_operations=max_operations,
            continuous=continuous,
            config_version=_config_version(raw),
            admission_instances=_admissions(
                context.get("admission_instances"),
                continuous=continuous,
                bounded=True,
            ),
            source=source,
        )
    except (TypeError, ValueError):
        return None


def inherited_admissions(
    context: Mapping[str, Any], *, continuous: bool
) -> tuple[AdmissionInstance, ...] | None:
    """Fail softly while normalizing a legacy handoff's admission owners."""
    try:
        return _admissions(
            context.get("admission_instances"),
            continuous=continuous,
            bounded=True,
        )
    except (TypeError, ValueError):
        return None


def manifest_for_run(run: ProfilingRun | None) -> dict[str, Any] | None:
    """Return the immutable catalogue projection for one parsed run."""
    return None if run is None else run.manifest.to_catalogue_payload()


def manifest_for_measurement(measurement: Measurement | None) -> dict[str, Any] | None:
    """Return the retained run snapshot this measurement was selected under."""
    return None if measurement is None else measurement.run_manifest.to_catalogue_payload()
