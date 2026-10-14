"""Cached control policy and per-run admission budgets for one producer process."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from .export import close_sink
from .models import ControlState, ProfilingRun
from .runs import _truthy, parse_run
from .schema import (
    ENV_PROFILING_ENABLED,
    ENV_PROFILING_MODE,
    ENV_PROFILING_RUN,
    MODE_ADMIN,
    MODE_DISABLED,
    MODE_ENVIRONMENT,
    PRODUCER_INSTANCE_ID,
    PROFILING_MODES,
    STOP_REASON_UNAVAILABLE,
)

logger = logging.getLogger("cloud_agent_shared.operation_profiling")


@dataclass(frozen=True)
class _Published:
    """One immutable snapshot, swapped whole."""

    state: ControlState
    run: ProfilingRun | None
    #: Monotonic order of publication in this process, so a reader can tell
    #: whether the configuration it is acting on is still the current one
    #: without comparing any remote clock.
    generation: int


class RuntimePolicyStore:
    """Inside-process, thread-safe holder for the current collection config.

    Two properties, and both are about the same failure -- a delayed answer
    putting a stopped experiment back into force:

    * **A snapshot is replaced whole.** A reader never sees the new run id
      beside the previous run's operations, target or deadline, because there
      is no moment at which the two are separate objects.
    * **A version never goes backwards.** ``publish`` refuses a snapshot older
      than the one already applied, so a reply that overtook a newer one on the
      wire cannot undo it.

    There is no lease and no renewal. The configuration is what it is until the
    control source says otherwise, and the experiment's own absolute deadline is
    what ends it -- including for a node that never hears anything again. The
    one exception is :meth:`suspend`, for a process that knows its own read of
    the source just failed.
    """

    def __init__(self) -> None:
        """Start with no configuration: nothing may collect until one arrives."""
        self._lock = threading.Lock()
        self._state: _Published | None = None
        self._generation = 0
        self._version = -1

    def publish(self, state: ControlState, *, run: ProfilingRun | None = None) -> _Published:
        """Replace the snapshot, unless it is older than what is already applied."""
        with self._lock:
            if state.config_version < self._version and self._state is not None:
                return self._state
            parsed = run if run is not None else state.run
            if state.run is not None and parsed is not None and parsed.run_id != state.run.run_id:
                # A caller may hand over an already-parsed descriptor, but it
                # must describe the run the state names. Publishing a mismatch
                # would create exactly the torn snapshot this class prevents.
                raise ValueError("run descriptor does not describe the state's run")
            self._generation += 1
            self._version = state.config_version
            self._state = _Published(state=state, run=parsed, generation=self._generation)
            return self._state

    def suspend(self, *, stop_reason: str = STOP_REASON_UNAVAILABLE) -> _Published:
        """Stop selecting under the applied configuration, keeping its version.

        For a process that can no longer read its control source and must not
        keep collecting on the strength of its last answer. :meth:`publish`
        cannot say that: such a caller has no version of its own to state, and a
        "no run" below the applied version is refused as a rollback -- so the
        fail-closed answer was silently dropped and the run stayed in force.

        The version is kept rather than advanced, so the source's next answer is
        applied as usual: the same configuration once it can be read again, or a
        newer one. An older answer is still refused.
        """
        with self._lock:
            base = self._state.state if self._state is not None else ControlState(mode=MODE_ADMIN)
            state = replace(
                base,
                config_version=max(self._version, 0),
                run=None,
                stop_reason=stop_reason,
            )
            self._generation += 1
            self._version = state.config_version
            self._state = _Published(state=state, run=None, generation=self._generation)
            return self._state

    def clear(self) -> None:
        """Forget the configuration entirely. Tests call this."""
        with self._lock:
            self._state = None
            self._generation = 0
            self._version = -1

    def published(self) -> _Published | None:
        """Return the current snapshot, or ``None`` when none was installed."""
        with self._lock:
            return self._state

    def run(self) -> ProfilingRun | None:
        """Return the experiment this process may collect for, if any."""
        with self._lock:
            return self._state.run if self._state is not None else None

    def control(self) -> ControlState:
        """Return the configuration this process is acting on."""
        with self._lock:
            if self._state is None:
                return ControlState()
            return self._state.state

    def admits(self, run_id: str, *, operations: frozenset[str] | None = None) -> bool:
        """Whether an inherited selection is still one this process may collect for.

        This is how a downstream producer answers "may I collect for this
        selection". The caller names the run it inherited; this process answers
        only if that is the run the configuration still names and that run still
        selects one of the operations in play. A selection from a previous
        experiment is refused rather than reported as if this experiment had
        made it.

        A running measurement is deliberately *not* checked against this: an
        operation that already started finishes and delivers under the
        configuration that selected it. This gate is only about *new* work.
        """
        with self._lock:
            state = self._state
        if state is None or state.run is None or state.run.run_id != run_id:
            return False
        return not operations or bool(state.run.operations & operations)


class PolicyProvider(Protocol):
    """Supplies the collection configuration for whichever source this process uses.

    The control plane implements it over the database and the host agent over
    the internal configuration exchange; the collector only ever calls it. A
    provider that cannot answer publishes nothing, which reads as "no run" --
    the safe direction, because a control source that is unreachable must never
    be mistaken for one that said "collect".
    """

    def snapshot(self) -> ControlState:
        """Return the configuration this process should act on."""
        ...


#: The installed store, and the provider that fills it. Both process-global for
#: the same reason the collector's other state is: one process has one control
#: source, and a second one would make "is collection on" depend on which thread
#: asked.
_STORE = RuntimePolicyStore()


_STORE_LOCK = threading.Lock()


_PROVIDER: PolicyProvider | None = None


#: How many times the installed provider has been read. A control-plane route
#: that answers before its background loop's first pass must not report
#: "unavailable" just because nobody has read the database yet, so the first
#: question forces one read. One integer, checked only when the store is empty.
_REFRESHED = 0


def install_policy_provider(provider: PolicyProvider | None) -> None:
    """Install the process's control provider and drop the cached config.

    Replacing a provider also drops the load cache, so the very next call
    re-reads the control source rather than answering from the previous one's
    answer. A caller that installs a provider and then collects without
    publishing anything collects nothing -- there is no "previous run" to fall
    back to, which is what makes a Stop survive a restart.
    """
    global _PROVIDER, _REFRESHED
    with _STORE_LOCK:
        _PROVIDER = provider
        _REFRESHED = 0
    reset_policy()
    if provider is not None:
        refresh_policy()


def policy_provider() -> PolicyProvider | None:
    """Return the installed provider, for the process that owns it."""
    with _STORE_LOCK:
        return _PROVIDER


def policy_store() -> RuntimePolicyStore:
    """Return the process's policy store.

    Callers that own the control source (the control plane's sync loop, the
    host's configuration client) publish into it; the collector only reads it.
    """
    return _STORE


def refresh_policy() -> _Published:
    """Read the installed provider once and publish what it answered.

    Provider failures are published as "no run" rather than raised: a control
    source that is unreachable must not be read as an experiment, and it must
    not fail the business request that was merely looking for one. The run is
    withdrawn at the applied version (see :meth:`RuntimePolicyStore.suspend`),
    because a fresh version-0 "no run" would be refused as a rollback.
    """
    global _REFRESHED
    with _STORE_LOCK:
        provider = _PROVIDER
        _REFRESHED += 1
    if provider is None:
        return _STORE.publish(ControlState())
    try:
        state = provider.snapshot()
    except Exception:
        logger.warning("operation profiling control source is unavailable", exc_info=True)
        return _STORE.suspend()
    return _STORE.publish(state)


def reset_policy() -> None:
    """Clear the published configuration and the load cache. Tests call this."""
    global _CACHE
    with _CACHE_LOCK:
        _CACHE = None
    _STORE.clear()


@dataclass(frozen=True)
class _Loaded:
    """Cached answer to "may this process collect, and for which run".

    ``mode`` is the deployment's control source rather than an experiment's
    switch, so it is answered even when there is no run -- that is what lets the
    Admin surface say "this deployment is managed by configuration" instead of
    the false "off" a boolean would produce.

    ``generation`` is the policy publication this answer was derived from. An
    admin-mode process re-reads on every load and compares one integer; every
    other process never re-reads at all.
    """

    run: ProfilingRun | None
    reason: str
    mode: str = MODE_DISABLED
    control: ControlState = field(default_factory=ControlState)
    generation: int = 0


_CACHE: _Loaded | None = None


#: Guards the derived read cache. Never held while parsing or logging.
_CACHE_LOCK = threading.Lock()


#: Guards the parse-once state below. Parsing happens outside it, because the
#: parse itself reads the environment and may log.
_MODE_LOCK = threading.Lock()


#: Parsed once per process: the deployment's mode, and the static descriptor it
#: names. Both are deployment configuration, which a restart is what changes.
_MODE: str | None = None


_INVALID_MODE = ""
#: Whether the refused mode above has been reported. The refusal is kept for
#: as long as the process runs, but it is said once: every request and claim
#: poll asks for the mode, and a warning on each would be a log write on the
#: path this module promises to keep cheap.
_INVALID_MODE_REPORTED = False


_ENV_RUN: ProfilingRun | None = None


_ENV_RUN_READ = False


_BUDGET_LOCK = threading.Lock()


@dataclass
class _RunBudget:
    """One experiment's per-process root-operation allowance.

    Per run rather than per process. Two syncs of the *same* configuration must
    not hand back slots that experiment already spent, while an administrator's
    *next* experiment must start from a full allowance rather than inheriting
    the previous one's exhaustion.
    """

    max_operations: int
    used: int = 0


#: Keyed by run id and pruned when a run is dropped, so the table is bounded by
#: the experiments a process actually selected rather than by its uptime.
_BUDGETS: dict[str, _RunBudget] = {}


def profile_mode() -> str:
    """Return which control source this deployment allows.

    Read once per process. The value is deployment configuration: changing it is
    a redeploy by definition, and the whole point of the feature is that
    changing it is *not* how an experiment is started or stopped.

    An unrecognized value is reported as ``disabled`` with a warning rather
    than guessed at. "Refuse to collect until somebody fixes the spelling" is
    the safe reading of a typo; treating it as ``environment`` could silently
    enable collection from a stale descriptor. The warning is written once per
    process, by whichever caller asks first.
    """
    global _MODE, _INVALID_MODE_REPORTED
    with _MODE_LOCK:
        if _MODE is None:
            _MODE = _read_mode()
        mode = _MODE
        # Claimed under the lock, so concurrent first callers report it once.
        invalid = "" if _INVALID_MODE_REPORTED else _INVALID_MODE
        if invalid:
            _INVALID_MODE_REPORTED = True
    if invalid:
        # Reported outside the lock: a log handler that itself asked about
        # profiling would deadlock the process it is reporting on.
        logger.warning(
            "%s=%r is not a profiling mode (%s); profiling stays off",
            ENV_PROFILING_MODE,
            invalid,
            ", ".join(PROFILING_MODES),
        )
    return mode


def _read_mode() -> str:
    """Parse the mode out of the environment exactly once.

    Called with :data:`_MODE_LOCK` held, so it must not log: a logging handler
    that asked about profiling would otherwise deadlock. The refusal is
    therefore returned and reported by the caller.
    """
    raw = (os.environ.get(ENV_PROFILING_MODE) or "").strip().lower()
    if not raw:
        # Keep deliberately enabled static experiments working. Otherwise the
        # Admin button is available without deployment setup; the absence of
        # an active persisted run, not the absence of this variable, means off.
        return MODE_ENVIRONMENT if _truthy(os.environ.get(ENV_PROFILING_ENABLED)) else MODE_ADMIN
    global _INVALID_MODE
    if raw not in PROFILING_MODES:
        # Remembered rather than logged here: the caller reports it outside the
        # lock, because a log handler that itself asked about profiling would
        # deadlock the process it is reporting on.
        _INVALID_MODE = raw
        return MODE_DISABLED
    return raw


def _environment_run() -> ProfilingRun | None:
    """Return the run the static deployment descriptor names, if any.

    Parsed once per process, exactly as before this module grew a control
    source: the environment is deployment configuration and cannot change
    without a restart, so re-reading it would only add a JSON parse to every
    request that asks whether profiling is on.

    The descriptor is parsed *outside* the cache lock. Parsing reads the
    environment and logs, and holding a lock that the collector's read path also
    takes while doing either is how a warning handler that itself asks about
    profiling deadlocks the process it is reporting on.
    """
    global _ENV_RUN, _ENV_RUN_READ
    with _MODE_LOCK:
        if _ENV_RUN_READ:
            return _ENV_RUN
    run: ProfilingRun | None = None
    if _truthy(os.environ.get(ENV_PROFILING_ENABLED)):
        raw = os.environ.get(ENV_PROFILING_RUN) or ""
        try:
            run = parse_run(raw)
        except ValueError as exc:
            # A switch without a usable run is not an experiment. Keep the
            # business path identical and say why once per process.
            logger.warning(
                "operation profiling is enabled but its run descriptor is unusable "
                "(%s); continuing without profiling",
                exc,
            )
            run = None
    with _MODE_LOCK:
        _ENV_RUN = run
        _ENV_RUN_READ = True
    return run


def _load() -> _Loaded:
    """Return the current control answer, re-deriving it when policy changed.

    Cached per *publication* rather than once per process. A process whose
    policy cannot change -- every static deployment, and every environment-mode
    process -- therefore parses its descriptor exactly once, while a process
    under Admin control answers from the snapshot the database just delivered.
    The generation is a single integer read, which is what keeps the off path
    as cheap as it was.
    """
    global _CACHE
    mode = profile_mode()
    if mode != MODE_ADMIN:
        with _CACHE_LOCK:
            if _CACHE is not None:
                return _CACHE
            run = _environment_run() if mode == MODE_ENVIRONMENT else None
            _CACHE = _Loaded(
                run,
                "active" if run is not None else "not_enabled",
                mode=mode,
            )
            return _CACHE

    published = _STORE.published()
    if published is None and policy_provider() is not None:
        # Nobody has read the control source in this process yet -- a route
        # answering before the background loop's first pass. Read it once, now,
        # rather than reporting "no control row" for a deployment that has one.
        with _STORE_LOCK:
            refreshed = _REFRESHED
        if refreshed == 0:
            refresh_policy()
            published = _STORE.published()
    generation = published.generation if published is not None else 0
    with _CACHE_LOCK:
        if _CACHE is not None and _CACHE.generation == generation:
            return _CACHE
    state = published.state if published is not None else ControlState(mode=MODE_ADMIN)
    run = published.run if published is not None else None
    loaded = _Loaded(
        run,
        "active" if run is not None else "not_enabled",
        mode=MODE_ADMIN,
        control=state,
    )
    with _CACHE_LOCK:
        _CACHE = replace(loaded, generation=generation)
        return _CACHE


def active_run() -> ProfilingRun | None:
    """Return the live run descriptor, or ``None`` when profiling is off.

    This answers "is an experiment in force", which is what a producer checks
    before deciding to look more closely. A Stop is observed here -- on the next
    selection -- and never as a withdrawal of work already selected.
    """
    return _load().run


def control_state() -> ControlState:
    """Return the control state this process is acting on.

    Bounded and non-secret: mode, control revision, run id and the authority
    instant. It is what a host reports back as its applied policy and what the
    Admin surface reads to tell "starting" from "collecting".
    """
    return _load().control


def reason() -> str:
    """Return ``not_enabled`` or ``active`` for startup diagnostics."""
    return _load().reason


def _claim_budget(run: ProfilingRun) -> bool:
    """Reserve one root-operation slot for this run, or refuse when used up."""
    if run.continuous:
        return True
    with _BUDGET_LOCK:
        budget = _BUDGETS.get(run.run_id)
        if budget is None:
            budget = _RunBudget(max_operations=run.max_operations)
            _BUDGETS[run.run_id] = budget
        if budget.max_operations <= budget.used:
            return False
        budget.used += 1
        return True


def _release_budget(run_id: str) -> None:
    """Return a reserved slot for a measurement that produced no record."""
    with _BUDGET_LOCK:
        budget = _BUDGETS.get(run_id)
        if budget is not None and budget.used > 0:
            budget.used -= 1


def budget_state(run_id: str) -> dict[str, Any]:
    """Return this process's allowance and usage for one run.

    Reported to the Admin surface so "the experiment collected nothing" can be
    told apart from "this instance spent its share", which the design requires
    to be distinguishable. The scope is stated in the answer because the number
    is a per-producer-instance count and a page that presented it as a
    deployment-wide cap would be describing a quota nobody enforces.
    """
    with _BUDGET_LOCK:
        budget = _BUDGETS.get(run_id)
        if budget is None:
            return {
                "scope": "producer_instance",
                "instance": PRODUCER_INSTANCE_ID,
                "max_operations": None,
                "used": 0,
                "exhausted": False,
            }
        return {
            "scope": "producer_instance",
            "instance": PRODUCER_INSTANCE_ID,
            "max_operations": budget.max_operations,
            "used": budget.used,
            "exhausted": budget.used >= budget.max_operations,
        }


def reset_cache() -> None:
    """Drop every cached policy answer and stop the sink. Tests call this."""
    global _CACHE, _MODE, _ENV_RUN, _ENV_RUN_READ, _INVALID_MODE, _INVALID_MODE_REPORTED
    global _REFRESHED
    with _CACHE_LOCK:
        _CACHE = None
    with _MODE_LOCK:
        _MODE = None
        _ENV_RUN = None
        _ENV_RUN_READ = False
        _INVALID_MODE = ""
        _INVALID_MODE_REPORTED = False
    with _STORE_LOCK:
        _REFRESHED = 0
    with _BUDGET_LOCK:
        _BUDGETS.clear()
    close_sink()
