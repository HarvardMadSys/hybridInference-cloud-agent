"""Wire constants, operation boundaries, and producer process identity."""

from __future__ import annotations

import os
import re
import socket
import uuid

#: Legacy static-experiment switch. Absent or false leaves control to Admin.
ENV_PROFILING_ENABLED = "AGENT_OPERATION_PROFILING_ENABLED"


#: JSON descriptor for the current profiling run. See :class:`ProfilingRun`.
ENV_PROFILING_RUN = "AGENT_OPERATION_PROFILING_RUN"


#: Which control source this deployment allows. One of :data:`PROFILING_MODES`.
#: Absent or blank defaults to Admin control, with collection off until an
#: administrator enables a run. An explicitly enabled legacy static experiment
#: keeps its environment mode. This optional override is read once per process;
#: it selects the control source, never the per-experiment switch itself.
ENV_PROFILING_MODE = "AGENT_OPERATION_PROFILING_MODE"


#: Optional JSONL path. Each finished measurement is appended as one line.
ENV_PROFILING_SINK = "AGENT_OPERATION_PROFILING_SINK"


#: The three answers to "who decides whether this deployment collects".
#:
#: ``disabled``  -- nobody does. Admin shows the control as unavailable rather
#:                  than offering a Start that would do nothing.
#: ``environment`` -- only the static ``ENABLED``/``RUN`` pair does, re-read
#:                  only when the process restarts. Admin says the deployment
#:                  is managed by configuration and offers no fake Stop.
#: ``admin``     -- only the persisted control state does. ``ENABLED=false``
#:                  no longer vetoes an Admin Start, and no old ``RUN`` may be
#:                  used as a fallback after an Admin Stop: a Stop has to mean
#:                  stopped, and a leftover descriptor silently re-enabling a
#:                  lapsed experiment is exactly the "off that is not off" the
#:                  design forbids.
MODE_DISABLED = "disabled"


MODE_ENVIRONMENT = "environment"


MODE_ADMIN = "admin"


#: Declared explicitly rather than derived, so a typo in the environment is
#: refused instead of being read as "the default".
PROFILING_MODES = (MODE_DISABLED, MODE_ENVIRONMENT, MODE_ADMIN)


#: Who rooted a run. A record carries this so a reader can tell a static
#: env experiment from one an administrator started, which is what makes the
#: "no control state" compatibility answer possible instead of a guess.
SOURCE_ENVIRONMENT = "environment"


SOURCE_ADMIN = "admin"


#: Major is bumped when a reader of an older record would misread a new one;
#: minor when fields are only added. v2 is additive over v1: ``outcome`` joins
#: ``startup_outcome``, and ``producer`` names whichever producer rooted the
#: operation instead of always the runner. A v1 reader can still consume a v2
#: record for ``attempt.startup`` by ignoring fields it does not know.
SCHEMA_VERSION = 3


#: Version of the *boundaries* this record's operation measured. Bumped when a
#: start or end point moves, so two records with the same operation name are
#: never compared as if they measured the same span.
BOUNDARY_VERSION = 1


#: How far the measured span reaches. A server-side record never covers the
#: user's own wait, and saying so in the data is what stops a reader from
#: presenting it as end to end.
COVERAGE_HOST_STARTUP = "host_startup"


COVERAGE_SERVER_REQUEST = "server_request"


COVERAGE_BROWSER_INTERACTION = "browser_interaction"


#: The coverage each root operation's boundaries actually reach. Filled in
#: below, once the operation names themselves are declared.
COVERAGE_BY_OPERATION: dict[str, str] = {}


BOUNDARIES_BY_OPERATION: dict[str, tuple[str, str]] = {
    "attempt.startup": ("runner.claim.request", "supervisor.prompt.dispatched"),
    "files.list": (
        "control_plane.operation.selected",
        "control_plane.business_result.validated",
    ),
    "files.open": (
        "control_plane.operation.selected",
        "control_plane.business_result.validated",
    ),
    "terminal.open": (
        "control_plane.operation.selected",
        "control_plane.business_result.validated",
    ),
}


#: The runner's root operation. Only this operation keeps the v1
#: ``startup_outcome`` alias, because that name is a claim about a startup.
STARTUP_OPERATION = "attempt.startup"


#: Alias carrying the same value as ``outcome``, written only for
#: ``attempt.startup``. A file read is not a startup and must not say it is.
STARTUP_OUTCOME_ALIAS = "startup_outcome"


COVERAGE_BY_OPERATION[STARTUP_OPERATION] = COVERAGE_HOST_STARTUP


#: Producer name for a record whose root is the runner process.
DEFAULT_PRODUCER = "runner"


#: Header a selected root operation's context travels in. It is measurement
#: metadata, never a credential, and every producer re-validates it
#: against its own switch, run and deadline.
PROFILING_CONTEXT_HEADER = "X-Agent-Operation-Profiling"


#: Headers a producer answers in when its response body cannot carry the
#: answer -- a terminal stream has already started sending the terminal's own
#: bytes. Lower case because that is how an ASGI response exposes headers.
PROFILING_ANSWER_CALL_ID_HEADER = "x-agent-profiling-call-id"


PROFILING_ANSWER_RECORDED_HEADER = "x-agent-profiling-recorded"


PROFILING_ANSWER_TRUNCATED_HEADER = "x-agent-profiling-truncated"


PROFILING_ANSWER_STAGES_HEADER = "x-agent-profiling-stages"


#: Which operation the producer decided this call was, when the context offered
#: candidates. A file path is collected before its kind is known.
PROFILING_ANSWER_OPERATION_HEADER = "x-agent-profiling-operation"


#: Longest context a caller may hand over. The context carries five bounded
#: strings, so anything larger is a malformed or hostile value, not a selection.
MAX_CONTEXT_CHARS = 2048


#: How long a control-plane instance remembers a selection while a browser
#: interaction continues across requests (a terminal open is a list, a create
#: and a stream). Beyond the run's own deadline the selection is gone.
SELECTION_TTL_S = 120.0


#: Ceiling on remembered selections per process, so a client that never
#: finishes an interaction cannot grow the table without bound.
MAX_SELECTIONS = 512


#: Run ids are used in Admin URL path segments as well as database keys.
#: Accepting slashes makes a descriptor valid but its summary route
#: unreachable after URL decoding.
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


#: Supplied by the control plane as the fallback root stage name when no
#: operation is inherited. Kept generic deliberately: an unmarked file or
#: terminal measurement is not a startup, and must not be labelled one.
DEFAULT_ROOT_STAGE = "operation"


#: The only reason a record states for collection ending mid-flight: the
#: experiment reached its own deadline. A Stop does not truncate work that has
#: already started, so there is no second reason to declare.
STOP_REASON_EXPIRED = "expired"


#: Why a configuration is not collecting, when it is not. Shown by the page.
STOP_REASON_STOPPED = "stopped"
#: Why a process stopped selecting without being told to: it could not read its
#: control source. The source's next answer replaces it.
STOP_REASON_UNAVAILABLE = "control_unavailable"


#: Per-measurement span ceiling; the design budgets 128 spans per attempt.
MAX_SPANS = 128


#: Attribute strings are truncated here. The design forbids prompts, tokens,
#: environment variables, command lines and raw error text in a record.
MAX_ATTRIBUTE_CHARS = 200


#: Longest run id a descriptor may declare. The run id is a top-level record
#: field rather than a bounded attribute, so it is capped where the descriptor
#: is read -- long before any request carries it across a process boundary.
#: Violating it disables profiling instead of failing a startup.
MAX_RUN_ID_CHARS = 128


#: How many finished records may wait for the export thread before the sink
#: starts dropping. Reaching this means the sink cannot keep up with the
#: experiment, which is reported with the drops rather than hidden.
SINK_QUEUE_MAX = 256


#: Fields an experiment may scope a run by. A descriptor naming anything else
#: is rejected outright: an unknown filter would silently never match, and a
#: run that measures nothing is worse than a run that refuses to start.
TARGET_KEYS = frozenset({"host", "job_id", "thread_id", "sandbox_id"})


#: Sub-stages another producer may contribute to one local stage. Bounded
#: because the list crosses a process boundary and is not trusted.
MAX_REMOTE_STAGES = 32


_TRUTHY = {"1", "true", "yes", "on"}


#: Every outcome a root operation may report. The runner's startup outcomes are
#: about an attempt's fate; the others are about the endpoint a platform
#: operation reached and are what a file or terminal request reports. One list,
#: because the schema is one schema -- but no producer borrows the other's
#: words: a startup is never "completed" and a file read is never "dispatched".
OUTCOMES = frozenset(
    {
        # attempt.startup
        "dispatched",
        "failed",
        "cancelled",
        "lease_lost",
        "incomplete",
        # files.list, files.open, terminal.open
        "completed",
        # A newer request replaced this one before its result reached the user.
        "superseded",
    }
)


#: Retained for callers written against the first slice.
_OUTCOMES = OUTCOMES


def _instance_id() -> str:
    """Return a stable identifier for this producer process."""
    try:
        host = socket.gethostname()
    except OSError:  # pragma: no cover - hostname lookup is effectively local
        host = "unknown-host"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


#: Every producer has its own monotonic clock domain. Timestamps from two
#: domains are never subtracted from each other; they are correlated by
#: measurement id instead.
CLOCK_DOMAIN_ID = f"host-{uuid.uuid4().hex[:12]}"


#: Identifies one runner process across restarts.
PRODUCER_INSTANCE_ID = _instance_id()
