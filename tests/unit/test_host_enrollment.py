"""Joining the pool, staying in it, and being told to leave.

The control plane has had `/v1/agent/host/register` and `/heartbeat` since
phase G; nothing on the host side called either. Everything hanging off a host
row was therefore inert — capacity unreported, `drain` unread, `offline`
meaningless, and the `broker_url` from which files and terminals are served
never supplied.

What is checked here is the part that decides whether work is taken, because
that is where a wrong answer costs an attempt:

* draining stops claims and does not exit — the operator may undrain;
* a rejected credential stops claims permanently, and is distinguished from
  an unreachable control plane, which must not;
* a deployment with no pool keeps working exactly as before.
"""

from __future__ import annotations

import json
import pathlib
import re
import threading
import time
from typing import Any

import pytest

from cloud_agent import host_pool
from cloud_agent.schemas import HostHeartbeatRequest
from cloud_agent_host import enrollment, runner
from cloud_agent_shared.shared_protocol import HOST_LAUNCH_ERROR_MAX_CHARS


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture()
def credential_file(tmp_path, monkeypatch):
    path = tmp_path / "credential"
    monkeypatch.setenv(enrollment.ENV_CREDENTIAL_FILE, str(path))
    monkeypatch.delenv(enrollment.ENV_HOST_CREDENTIAL, raising=False)
    monkeypatch.delenv(enrollment.ENV_ENROLL_TOKEN, raising=False)
    return path


# ---------------------------------------------------------------------------
# Enrolling
# ---------------------------------------------------------------------------


def test_a_deployment_with_no_pool_is_left_alone(credential_file) -> None:
    """No credential and no token means the single-host shape, not an error.

    This predates the pool and is still the supported way to run one machine.
    A runner that refused to start without enrolling would break every
    existing deployment on upgrade.
    """
    assert enrollment.ensure_enrolled(base_url="http://cp", host="h1") is None


def test_a_token_is_spent_for_a_credential(credential_file, monkeypatch) -> None:
    monkeypatch.setenv(enrollment.ENV_ENROLL_TOKEN, "enroll-once")
    monkeypatch.setenv("AGENT_PHYSICAL_HOST", "gpu-box")
    sent: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        # `enrolling`, which is what the control plane actually answers a
        # registration with: the credential is issued, but the machine has not
        # heartbeated yet. See `Storage.register_host`.
        return _Response({"host": "h1", "credential": "hc_secret", "status": "enrolling"})

    monkeypatch.setattr(enrollment.urllib.request, "urlopen", fake_urlopen)

    identity = enrollment.ensure_enrolled(base_url="http://cp", host="h1", slots_total=4)

    assert identity is not None
    assert identity.credential == "hc_secret"
    assert sent["body"]["enroll_token"] == "enroll-once"
    assert sent["body"]["slots_total"] == 4
    assert sent["body"]["name"] == "h1"
    assert sent["body"]["labels"] == {"physical_host": "gpu-box"}
    assert sent["url"].endswith("/v1/agent/host/register")


def test_the_credential_is_stored_unreadable_to_others(credential_file, monkeypatch) -> None:
    """This machine runs untrusted repository code next door."""
    monkeypatch.setenv(enrollment.ENV_ENROLL_TOKEN, "enroll-once")
    monkeypatch.setattr(
        enrollment.urllib.request,
        "urlopen",
        lambda request, timeout=None: _Response({"host": "h1", "credential": "hc_secret"}),
    )

    enrollment.ensure_enrolled(base_url="http://cp", host="h1")

    assert credential_file.read_text() == "hc_secret"
    assert credential_file.stat().st_mode & 0o077 == 0, "the credential is readable by others"


def test_a_file_left_at_the_old_temporary_name_cannot_widen_the_credential(
    credential_file,
) -> None:
    stale = credential_file.with_suffix(".tmp")
    stale.write_text("stale")
    stale.chmod(0o644)

    enrollment.store_credential("hc_secret")

    assert credential_file.read_text() == "hc_secret"
    assert credential_file.stat().st_mode & 0o777 == 0o600
    assert stale.read_text() == "stale"


def test_a_planted_link_cannot_receive_the_credential(credential_file, tmp_path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("")
    credential_file.with_suffix(".tmp").symlink_to(elsewhere)

    enrollment.store_credential("hc_secret")

    assert elsewhere.read_text() == ""
    assert not credential_file.is_symlink()
    assert credential_file.read_text() == "hc_secret"


def test_storing_leaves_no_temporary_file_and_a_new_directory_private(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "state" / "credential"
    monkeypatch.setenv(enrollment.ENV_CREDENTIAL_FILE, str(path))

    enrollment.store_credential("hc_first")
    enrollment.store_credential("hc_second")

    assert path.read_text() == "hc_second"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert sorted(entry.name for entry in path.parent.iterdir()) == ["credential"]


def test_a_failed_store_removes_its_temporary_file(credential_file, monkeypatch) -> None:
    def refuse(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(enrollment.os, "replace", refuse)
    with pytest.raises(OSError, match="disk full"):
        enrollment.store_credential("hc_secret")

    assert list(credential_file.parent.iterdir()) == []


def test_a_stored_credential_is_not_spent_again(credential_file, monkeypatch) -> None:
    """Restarting must not consume a second enrollment token."""
    credential_file.write_text("hc_existing")
    monkeypatch.setenv(enrollment.ENV_ENROLL_TOKEN, "enroll-once")

    def refuse(request, timeout=None):  # pragma: no cover - must not be called
        raise AssertionError("registered again despite holding a credential")

    monkeypatch.setattr(enrollment.urllib.request, "urlopen", refuse)

    identity = enrollment.ensure_enrolled(base_url="http://cp", host="h1")
    assert identity is not None
    assert identity.credential == "hc_existing"


def test_registration_without_a_credential_in_the_answer_is_an_error(
    credential_file, monkeypatch
) -> None:
    """Returning `None` here would look like the no-pool shape and run on."""
    monkeypatch.setenv(enrollment.ENV_ENROLL_TOKEN, "enroll-once")
    monkeypatch.setattr(
        enrollment.urllib.request,
        "urlopen",
        lambda request, timeout=None: _Response({"host": "h1", "status": "online"}),
    )

    with pytest.raises(enrollment.EnrollmentError):
        enrollment.ensure_enrolled(base_url="http://cp", host="h1")


def test_replicas_sharing_a_token_enroll_once(credential_file, monkeypatch) -> None:
    """Three runner processes, one machine, one single-use token.

    The compose file starts three replicas sharing a credential file, a host
    name and an enrollment token. The control plane spends that token for
    exactly one of them, so unlocked the other two fail and crash-loop until a
    restart happens to read the file the winner wrote. It settles — and "a new
    host crash-loops on first start" is not a thing to make an operator learn
    to recognise.

    Threads rather than processes because the lock is on a shared file either
    way, and the assertion is about how many registrations reach the control
    plane.
    """
    monkeypatch.setenv(enrollment.ENV_ENROLL_TOKEN, "enroll-once")
    registrations = []
    lock = threading.Lock()

    def fake_urlopen(request, timeout=None):
        with lock:
            registrations.append(request.full_url)
            spent = len(registrations) > 1
        if spent:
            raise AssertionError("the enrollment token was presented twice")
        time.sleep(0.05)  # widen the window the lock has to close
        return _Response({"host": "h1", "credential": "hc_secret", "status": "enrolling"})

    monkeypatch.setattr(enrollment.urllib.request, "urlopen", fake_urlopen)

    results: list[object] = []
    threads = [
        threading.Thread(
            target=lambda: results.append(
                enrollment.ensure_enrolled(base_url="http://cp", host="h1")
            )
        )
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(registrations) == 1, f"{len(registrations)} replicas spent the token"
    assert all(r is not None and r.credential == "hc_secret" for r in results), results


# ---------------------------------------------------------------------------
# Speaking the control plane's language
# ---------------------------------------------------------------------------


def test_the_runner_and_the_control_plane_mean_the_same_statuses() -> None:
    """The mirror this package cannot import.

    `cloud_agent_host` runs on the runner host, which has neither the control
    plane nor its database, so the status vocabulary is copied rather than
    imported — and a copy is a thing that drifts. This is the assertion that
    makes the drift a failing test instead of a silent host.

    It exists because the copy said `active`, which is not one of the five
    statuses and is rejected outright by the schema's CHECK constraint. Every
    test built its own identity with that same invented value, so the suite
    agreed with the bug.
    """
    assert enrollment.CLAIMABLE_STATUSES == host_pool.CLAIMABLE_STATUSES
    assert enrollment.DEFAULT_STATUS in host_pool.HOST_STATUSES
    for status in enrollment.CLAIMABLE_STATUSES:
        assert status in host_pool.HOST_STATUSES, f"{status} is not a status the schema allows"


def test_every_status_this_module_writes_is_one_the_schema_allows() -> None:
    """The whole vocabulary, not just the constant.

    Fixing `CLAIMABLE_STATUSES` alone left a third `"active"` behind, in the
    fallback `register` uses when an older control plane answers without a
    status — a path no fake exercised, because every fake supplied one. This
    reads the module's own source so a fourth cannot be added quietly.
    """
    source = pathlib.Path(enrollment.__file__).read_text()
    # A word-literal following `status` on the same line, excluding the ones
    # that are dict keys or lookups (`"status":`, `.get("status")`) — those are
    # the name of the field, not a value it could take.
    written = set(re.findall(r'status[^\n]*?["\'](\w+)["\'](?![:)\]])', source))
    unknown = written - set(host_pool.HOST_STATUSES)
    assert not unknown, f"statuses no schema row can carry: {sorted(unknown)}"


def test_an_overlong_launch_error_is_trimmed_to_what_the_control_plane_accepts(monkeypatch) -> None:
    """A long reason must not cost the host its heartbeat.

    The control plane refuses a beat whose ``launch_error`` is longer than its
    schema allows, and refuses the whole beat: the host then misses its
    intervals, is marked offline and stops renewing its residencies, all over
    the length of an explanation. A failing Kata probe's stderr is that long.
    """
    bodies: list[dict[str, Any]] = []

    def record(url, body, bearer=""):
        bodies.append(body)
        return {"status": "online"}

    monkeypatch.setattr(enrollment, "_post", record)
    identity = enrollment.HostIdentity(host="h1", credential="c")

    enrollment.heartbeat(base_url="http://cp", identity=identity, launch_error="x" * 2000)
    enrollment.heartbeat(base_url="http://cp", identity=identity, launch_error="")
    enrollment.heartbeat(base_url="http://cp", identity=identity)

    trimmed = bodies[0]["launch_error"]
    assert trimmed == "x" * HOST_LAUNCH_ERROR_MAX_CHARS
    HostHeartbeatRequest(launch_error=trimmed)
    with pytest.raises(ValueError, match="launch_error"):
        HostHeartbeatRequest(launch_error=trimmed + "x")
    assert bodies[1]["launch_error"] == "", "an empty report must still clear the stored reason"
    assert bodies[2]["launch_error"] is None, "an unreported reason must stay unreported"


def test_a_registration_without_a_status_claims_nothing_yet(credential_file, monkeypatch) -> None:
    """An older control plane must not talk this runner into the pool."""
    monkeypatch.setenv(enrollment.ENV_ENROLL_TOKEN, "enroll-once")
    monkeypatch.setattr(
        enrollment.urllib.request,
        "urlopen",
        lambda request, timeout=None: _Response({"host": "h1", "credential": "hc_secret"}),
    )

    identity = enrollment.ensure_enrolled(base_url="http://cp", host="h1")

    assert identity is not None
    assert identity.status in host_pool.HOST_STATUSES
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)
    assert not beat.claimable, "assumed itself into the pool without a heartbeat"


def test_no_status_the_control_plane_can_send_is_unknown_here() -> None:
    """Every real status has an answer, and none of them raises.

    A status this side does not recognise must read as "do not claim" rather
    than as an error: the control plane is free to add one, and a runner that
    crashed on an unfamiliar word would turn a rollout into an outage.
    """
    for status in host_pool.HOST_STATUSES:
        identity = enrollment.HostIdentity(host="h1", credential="c", status=status)
        beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)
        assert beat.claimable is (status in host_pool.CLAIMABLE_STATUSES)


# ---------------------------------------------------------------------------
# Staying in, and being told to leave
# ---------------------------------------------------------------------------


def test_the_pool_decides_whether_to_claim() -> None:
    """`draining` is an operator saying "finish what you have"."""
    identity = enrollment.HostIdentity(host="h1", credential="c", status="online")
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)

    assert beat.claimable

    identity.status = "draining"
    assert not beat.claimable, "a draining host that keeps claiming makes drain mean nothing"


def test_a_heartbeat_does_not_silence_a_healthy_host(monkeypatch) -> None:
    """The shape the `active` bug actually had.

    A stored credential starts the runner at its default status, and the first
    heartbeat overwrites that with whatever the control plane says. If the two
    disagree the host claims for one interval and then stops for good — which
    reads as "the queue is broken", not as "this constant is wrong", and is
    why the default and the claimable set are pinned to the same vocabulary.
    """
    identity = enrollment.HostIdentity(host="h1", credential="c")
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)

    assert beat.claimable, "a restarted host with a credential must take work"

    monkeypatch.setattr(
        enrollment,
        "_post",
        lambda url, body, bearer="": {"status": "online"},
    )
    enrollment.heartbeat(base_url="http://cp", identity=identity)

    assert beat.claimable, "the host stopped claiming the moment the pool answered"


def test_each_heartbeat_recomputes_dynamic_host_capabilities(monkeypatch) -> None:
    identity = enrollment.HostIdentity(host="h1", credential="c")
    advertised = ["shared-sandbox-v1"]
    bodies = []
    beat = enrollment.HostHeartbeat(
        base_url="http://cp",
        identity=identity,
        capabilities=lambda: list(advertised),
    )

    def record(url, body, bearer=""):
        bodies.append(body)
        if len(bodies) == 1:
            advertised.append("shared-sandbox-deployment:target")
        else:
            beat._stop.set()
        return {"status": "online", "heartbeat_interval_seconds": 0.001}

    monkeypatch.setattr(enrollment, "_post", record)
    beat._run()

    assert bodies[0]["capabilities"] == ["shared-sandbox-v1"]
    assert bodies[1]["capabilities"] == [
        "shared-sandbox-v1",
        "shared-sandbox-deployment:target",
    ]


def test_the_readiness_marker_is_compared_with_the_value_at_startup(tmp_path) -> None:
    """Only a *change* is a reason to beat outside the schedule.

    The deployment writes `pending:<sha>` before it replaces the runners and
    the bare `<sha>` once every host check has passed. Treating a readable but
    unchanged marker as news would beat on every poll; treating an unreadable
    one as news would beat forever on a host whose mount is missing.
    """
    marker = tmp_path / "shared-live-ready"
    marker.write_text("pending:target\n")
    identity = enrollment.HostIdentity(host="h1", credential="c")
    beat = enrollment.HostHeartbeat(
        base_url="http://cp", identity=identity, readiness_marker=str(marker)
    )

    assert not beat._readiness_marker_moved(), "an unchanged marker woke the beat"

    marker.write_text("target\n")
    assert beat._readiness_marker_moved()
    assert not beat._readiness_marker_moved(), "the same value woke the beat twice"

    missing = enrollment.HostHeartbeat(
        base_url="http://cp", identity=identity, readiness_marker=str(tmp_path / "absent")
    )
    assert not missing._readiness_marker_moved(), "an unreadable marker woke the beat"


def test_a_torn_read_of_the_marker_is_not_a_change(tmp_path) -> None:
    """The deployment truncates this file in place, so a poll can see nothing.

    Keeping the inode is deliberate — every replica has it bind-mounted — so
    the empty window is inherent to the write. Beating on it would be a
    heartbeat per poll for the duration of the write, and it would reset the
    baseline to empty, making the value that landed look like a second change.
    """
    marker = tmp_path / "shared-live-ready"
    marker.write_text("pending:target\n")
    identity = enrollment.HostIdentity(host="h1", credential="c")
    beat = enrollment.HostHeartbeat(
        base_url="http://cp", identity=identity, readiness_marker=str(marker)
    )

    marker.write_text("")
    assert not beat._readiness_marker_moved(), "a torn read woke the beat"

    marker.write_text("target\n")
    assert beat._readiness_marker_moved(), "the value that landed was swallowed by the torn read"


def test_a_published_readiness_marker_pulls_the_next_beat_forward(tmp_path, monkeypatch) -> None:
    """The rollout fence must not wait out an interval for a fact already true.

    `sandbox_host.sh` publishes readiness by rewriting the file every runner
    mounts, and the control plane admits no rollout until a heartbeat carries
    that exact SHA back. With a plain `wait(interval)` the beat carrying it
    arrived up to one full interval after the host was ready: measured on
    staging, 15.2s of a 46s host deployment, and the whole remaining wait of
    the control job's 37.1s fence.
    """
    monkeypatch.setattr(enrollment, "READINESS_MARKER_POLL_S", 0.01)
    marker = tmp_path / "shared-live-ready"
    marker.write_text("pending:target\n")
    identity = enrollment.HostIdentity(host="h1", credential="c")
    # A short interval on purpose: a regression waits it out, and the point is
    # that it fails in seconds rather than in half a minute. The comparison
    # below is against the interval, not a number chosen for this one.
    identity.interval_s = 5.0
    beat = enrollment.HostHeartbeat(
        base_url="http://cp", identity=identity, readiness_marker=str(marker)
    )
    sent: list[float] = []
    started = time.monotonic()

    def record(url, body, bearer=""):
        sent.append(time.monotonic() - started)
        if len(sent) == 1:
            # The deployment passing its own checks, 200ms into the interval.
            threading.Timer(0.2, lambda: marker.write_text("target\n")).start()
        else:
            beat._stop.set()
        return {"status": "online", "heartbeat_interval_seconds": 5}

    monkeypatch.setattr(enrollment, "_post", record)
    beat._run()

    assert len(sent) == 2, "the marker change did not produce a heartbeat"
    assert sent[1] < identity.interval_s / 2, (
        f"waited {sent[1]:.1f}s of a {identity.interval_s}s interval for a marker "
        "published 0.2s in; that is the schedule, not the change"
    )


def test_the_runner_watches_the_marker_the_deployment_publishes(monkeypatch) -> None:
    """Both halves of the fence have to be wired to each other.

    `sandbox_host.sh` publishing the marker is only one half; the runner has to
    notice. A runner that mounts the marker and reads it on the beat alone
    leaves the rollout fence waiting for the next scheduled heartbeat, which is
    silent and is what the interval-sized wait above costs.

    The other half is that the capabilities are recomputed. `HostHeartbeat`
    accepts a static list as well as a callable, and the marker reaches the
    control plane *through* that list: given a static one, waking early would
    only send an answer computed once at startup, and the fence would wait out
    the interval anyway — the optimization silently becoming nothing.
    """
    monkeypatch.setenv("AGENT_SHARED_LIVE_READY_FILE", "/run/cloud-agent/shared-live-ready")

    assert runner._shared_live_ready_file() == "/run/cloud-agent/shared-live-ready"

    source = pathlib.Path(runner.__file__).read_text()
    pool = source[source.index("pool = HostHeartbeat(") : source.index("pool.start()")]
    assert "readiness_marker=_shared_live_ready_file()" in pool, (
        "the runner builds its host heartbeat without the marker it was given"
    )
    assert "capabilities=lambda:" in pool, (
        "the host heartbeat was given a static capability list, so a marker-driven beat "
        "would report the capabilities this runner started with"
    )


def test_without_a_marker_the_heartbeat_cadence_is_unchanged(monkeypatch) -> None:
    """A deployment that mounts no marker keeps the interval it always had."""
    identity = enrollment.HostIdentity(host="h1", credential="c")
    identity.interval_s = 0.3
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)

    started = time.monotonic()
    beat._wait_for_next_beat()

    assert time.monotonic() - started >= 0.25, "a host with no marker beat off-schedule"


def test_a_rejected_credential_stops_claims_for_good(monkeypatch) -> None:
    """401 and 403 name *this host*; there is nothing to retry."""
    identity = enrollment.HostIdentity(host="h1", credential="c")
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)

    def rejected(url, body, bearer=""):
        raise enrollment.HostRemoved("403")

    monkeypatch.setattr(enrollment, "_post", rejected)

    # No stop needed: a rejected credential returns out of the loop, which is
    # the behaviour being asserted.
    beat._run()

    assert beat.removed.is_set()
    assert not beat.claimable


def test_an_unreachable_control_plane_does_not_remove_the_host(monkeypatch) -> None:
    """The distinction that matters.

    A control plane restarting is not a host being retired, and treating them
    alike would empty the pool during every deploy. The control plane's own
    reaper decides when silence has gone on too long — duplicating that
    judgement here would give two components a vote on the same fact.
    """
    identity = enrollment.HostIdentity(host="h1", credential="c")
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)
    calls = {"n": 0}

    def unreachable(url, body, bearer=""):
        calls["n"] += 1
        # Stop the loop from inside, after it has been seen to retry. Patching
        # `wait`'s return value does not end it — the loop exits on
        # `is_set()`, so a fake that only returns True spins forever. It did,
        # for five minutes, before this was written the other way round.
        if calls["n"] >= 2:
            beat._stop.set()
        raise enrollment.EnrollmentError("connection refused")

    monkeypatch.setattr(enrollment, "_post", unreachable)
    # No real sleeping between passes; the retry behaviour is the subject, not
    # the interval.
    monkeypatch.setattr(beat._identity, "interval_s", 0.0)

    beat._run()

    assert calls["n"] >= 2, "gave up after one unreachable heartbeat"
    assert not beat.removed.is_set()
    assert beat.claimable


def test_the_interval_follows_the_control_plane(monkeypatch) -> None:
    """Both sides must agree on what a missed heartbeat is.

    The reaper counts missed intervals; a runner beating to its own constant
    would be marked offline by arithmetic rather than by absence.
    """
    identity = enrollment.HostIdentity(host="h1", credential="c")
    monkeypatch.setattr(
        enrollment,
        "_post",
        lambda url, body, bearer="": {"status": "online", "heartbeat_interval_seconds": 7.5},
    )

    enrollment.heartbeat(base_url="http://cp", identity=identity)

    assert identity.interval_s == 7.5


def test_the_heartbeat_reads_the_requested_executor_count_and_forgets_a_cleared_one(
    monkeypatch,
) -> None:
    """The supervisor sizes its pool from this; a cleared request must not linger."""
    identity = enrollment.HostIdentity(host="h1", credential="c")
    answers = iter(
        [
            {"status": "online", "slots_desired": 6},
            {"status": "online", "slots_desired": None},
            {"status": "online"},
            {"status": "online", "slots_desired": True},
        ]
    )
    monkeypatch.setattr(enrollment, "_post", lambda url, body, bearer="": next(answers))

    enrollment.heartbeat(base_url="http://cp", identity=identity)
    assert identity.slots_desired == 6
    enrollment.heartbeat(base_url="http://cp", identity=identity)
    assert identity.slots_desired is None
    identity.slots_desired = 6
    enrollment.heartbeat(base_url="http://cp", identity=identity)
    assert identity.slots_desired is None, "an absent field is no request, not the old one"
    enrollment.heartbeat(base_url="http://cp", identity=identity)
    assert identity.slots_desired is None, "a boolean is not a count"


def test_the_heartbeat_carries_the_settings_report_and_reads_the_admin_values(
    monkeypatch,
) -> None:
    """The answer replaces the settings whole; a value this host cannot apply is dropped."""
    identity = enrollment.HostIdentity(host="h1", credential="c")
    bodies: list[dict] = []
    answers = iter(
        [
            {"status": "online", "settings": {"agent_timeout_s": 900, "unknown": 1}},
            {"status": "online", "settings": {"terminal_max_sessions": 0}},
            {"status": "online"},
        ]
    )

    def post(url, body, bearer=""):
        bodies.append(body)
        return next(answers)

    monkeypatch.setattr(enrollment, "_post", post)

    enrollment.heartbeat(
        base_url="http://cp",
        identity=identity,
        settings_applied={"agent_timeout_s": 3600},
        settings_local={"agent_timeout_s": 3600},
    )
    assert bodies[0]["settings_applied"] == {"agent_timeout_s": 3600}
    assert bodies[0]["settings_local"] == {"agent_timeout_s": 3600}
    assert identity.settings == {"agent_timeout_s": 900}

    enrollment.heartbeat(base_url="http://cp", identity=identity)
    assert identity.settings == {}, "a value outside this host's bounds is not applied"

    identity.settings = {"agent_timeout_s": 900}
    enrollment.heartbeat(base_url="http://cp", identity=identity)
    assert identity.settings == {}, "a control plane with no settings leaves the host's own"


def test_the_beat_hands_on_the_settings_and_survives_a_failing_handler(monkeypatch) -> None:
    identity = enrollment.HostIdentity(host="h1", credential="c")
    seen: list[dict] = []
    beats = [0]
    reports = iter([({"agent_timeout_s": 3600}, {"agent_timeout_s": 3600}), ({}, {})])

    def on_settings(settings: dict) -> None:
        seen.append(settings)
        raise RuntimeError("broker exploded")

    beat = enrollment.HostHeartbeat(
        base_url="http://cp",
        identity=identity,
        settings_report=lambda: next(reports),
        on_settings=on_settings,
    )
    bodies: list[dict] = []

    def post(url, body, bearer=""):
        bodies.append(body)
        beats[0] += 1
        if beats[0] == 2:
            beat._stop.set()
        return {
            "status": "online",
            "settings": {"agent_timeout_s": 900},
            # Short, so the test does not wait out a real interval between beats.
            "heartbeat_interval_seconds": 0.01,
        }

    monkeypatch.setattr(enrollment, "_post", post)
    beat._run()

    assert [body["settings_applied"] for body in bodies] == [{"agent_timeout_s": 3600}, {}]
    assert seen == [{"agent_timeout_s": 900}, {"agent_timeout_s": 900}]


def test_a_callable_slot_count_is_read_fresh_on_every_beat(monkeypatch) -> None:
    """The supervisor's live executor count moves; the beat must carry the current one."""
    identity = enrollment.HostIdentity(host="h1", credential="c")
    live = [3]
    bodies: list[dict] = []
    beat = enrollment.HostHeartbeat(
        base_url="http://cp", identity=identity, slots_total=lambda: live[0]
    )

    def record(url, body, bearer=""):
        bodies.append(body)
        if len(bodies) == 1:
            live[0] = 5
        else:
            beat._stop.set()
        return {"status": "online", "heartbeat_interval_seconds": 0.001}

    monkeypatch.setattr(enrollment, "_post", record)
    beat._run()

    assert [body["slots_total"] for body in bodies] == [3, 5]


def test_wake_sends_the_next_beat_without_waiting_for_the_interval(monkeypatch) -> None:
    identity = enrollment.HostIdentity(host="h1", credential="c")
    identity.interval_s = 60.0
    beats = threading.Semaphore(0)
    count = [0]
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)

    def record(url, body, bearer=""):
        count[0] += 1
        if count[0] == 2:
            beat._stop.set()
        beats.release()
        return {"status": "online", "heartbeat_interval_seconds": 60}

    monkeypatch.setattr(enrollment, "_post", record)
    beat.start()
    assert beats.acquire(timeout=5), "the first beat is immediate"
    beat.wake()
    assert beats.acquire(timeout=5), "the second beat followed the wake, not the minute"
    beat.stop()
    assert count[0] == 2


def test_heartbeat_carries_retained_sandbox_observations_without_inferring_absence(
    monkeypatch,
) -> None:
    identity = enrollment.HostIdentity(host="h1", credential="c")
    sent: list[dict] = []

    def answer(url, body, bearer=""):
        sent.append(body)
        return {"status": "online"}

    monkeypatch.setattr(enrollment, "_post", answer)
    rows = [
        {
            "thread_id": "athr_1",
            "sandbox_id": "asbx_1",
            "container_id": "0123456789abcdef",
            "generation": 1,
            "activation_generation": 2,
            "running": False,
        }
    ]

    enrollment.heartbeat(base_url="http://cp", identity=identity, retained_sandboxes=rows)

    assert sent[0]["retained_sandboxes"] == rows


def test_heartbeat_delivers_fenced_idle_stop_commands(monkeypatch) -> None:
    identity = enrollment.HostIdentity(host="h1", credential="c")
    delivered: list[list[dict]] = []
    commands = [
        {
            "thread_id": "athr_1",
            "generation": 2,
            "activation_generation": 7,
        }
    ]
    monkeypatch.setattr(
        enrollment,
        "_post",
        lambda *_args, **_kwargs: {
            "status": "online",
            "retained_sandboxes_stop": commands,
        },
    )

    enrollment.heartbeat(
        base_url="http://cp",
        identity=identity,
        on_retained_sandboxes_stop=delivered.append,
    )

    assert delivered == [commands]


def test_a_nonsense_interval_is_ignored(monkeypatch) -> None:
    """Zero or negative would busy-loop against the control plane."""
    identity = enrollment.HostIdentity(host="h1", credential="c")
    before = identity.interval_s
    monkeypatch.setattr(
        enrollment,
        "_post",
        lambda url, body, bearer="": {"status": "online", "heartbeat_interval_seconds": 0},
    )

    enrollment.heartbeat(base_url="http://cp", identity=identity)

    assert identity.interval_s == before


def test_a_reset_connection_is_an_enrollment_error(monkeypatch) -> None:
    """The gap that killed the heartbeat thread on 2026-08-06.

    `_post` wrapped `urllib.error.URLError`, which covers a connection that
    could not be *made*. A control plane restarting mid-request resets one that
    already exists, and http.client raises `ConnectionResetError` from under
    urlopen — an OSError, not a URLError. It escaped this wrapper and therefore
    escaped `HostHeartbeat._run`'s `except EnrollmentError`, ending the thread.

    The existing retry test could not have caught it: it patches `_post`, which
    is the layer the gap was in.
    """

    def reset(request, timeout=None):
        raise ConnectionResetError(104, "Connection reset by peer")

    monkeypatch.setattr(enrollment.urllib.request, "urlopen", reset)

    with pytest.raises(enrollment.EnrollmentError):
        enrollment._post("http://cp/v1/agent/host/heartbeat", {}, bearer="c")


def test_the_heartbeat_thread_survives_an_unexpected_exception(monkeypatch) -> None:
    """A dead heartbeat thread is silent and permanent.

    Threads are not restarted, so an escaping exception means the host reports
    offline for the life of the process — while claiming work normally, because
    that loop catches its own errors. An operator sees a machine that looks
    dead and is not.
    """
    identity = enrollment.HostIdentity(host="h1", credential="c")
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:
            beat._stop.set()
        raise RuntimeError("something nobody wrote an `except` for")

    monkeypatch.setattr(enrollment, "heartbeat", boom)
    monkeypatch.setattr(beat._identity, "interval_s", 0.0)

    beat._run()

    assert calls["n"] >= 2, "an unexpected exception ended the heartbeat thread"
    assert not beat.removed.is_set(), "an unexpected exception must not look like removal"


def test_legacy_host_residency_client_is_removed() -> None:
    """The host cannot bypass attempt-fenced worker residency coordination."""
    assert not hasattr(enrollment, "establish_residency")
    assert not hasattr(enrollment, "release_residency")


def test_physical_server_is_reported_separately_from_enrolled_identity(monkeypatch):
    """Two deployments can name one server without sharing credentials."""
    calls = []
    monkeypatch.setenv("AGENT_PHYSICAL_HOST", " gpu-box ")
    monkeypatch.setattr(
        enrollment,
        "_post",
        lambda url, body, bearer="": calls.append((body, bearer)) or {"status": "online"},
    )
    identity = enrollment.HostIdentity(host="gpu-box-kata", credential="kata-only")
    enrollment.heartbeat(base_url="http://cp", identity=identity)
    assert calls[-1][0]["physical_host"] == "gpu-box"
    assert calls[-1][1] == "kata-only"
    assert identity.host == "gpu-box-kata"
    monkeypatch.delenv("AGENT_PHYSICAL_HOST")
    enrollment.heartbeat(base_url="http://cp", identity=identity)
    assert calls[-1][0]["physical_host"] is None


def test_capacity_is_read_fresh_on_every_beat(monkeypatch) -> None:
    """The age travels with the beat, not with the sample.

    A cached block re-read on the next beat must carry the age it has *now*,
    otherwise a stalled collector would keep presenting an old observation as
    current — which is the one failure this whole feature exists to expose.
    """
    identity = enrollment.HostIdentity(host="h1", credential="c")
    bodies: list[dict] = []
    ages = iter([10, 20])
    beat = enrollment.HostHeartbeat(
        base_url="http://cp",
        identity=identity,
        capacity_beats=lambda: {"schema_version": 1, "sample_age_ms": next(ages)},
    )

    def record(url, body, bearer=""):
        bodies.append(body)
        if len(bodies) == 2:
            beat._stop.set()
        return {"status": "online", "heartbeat_interval_seconds": 0.001}

    monkeypatch.setattr(enrollment, "_post", record)
    beat._run()

    assert [body["capacity"]["sample_age_ms"] for body in bodies] == [10, 20]


def test_a_host_with_no_sample_sends_no_capacity_at_all(monkeypatch) -> None:
    """``None`` omits the field; it never sends a placeholder block."""
    identity = enrollment.HostIdentity(host="h1", credential="c")
    bodies: list[dict] = []
    beat = enrollment.HostHeartbeat(
        base_url="http://cp",
        identity=identity,
        capacity_beats=lambda: None,
    )

    def record(url, body, bearer=""):
        bodies.append(body)
        beat._stop.set()
        return {"status": "online", "heartbeat_interval_seconds": 0.001}

    monkeypatch.setattr(enrollment, "_post", record)
    beat._run()

    assert bodies[0]["capacity"] is None
    assert bodies[0]["slots_total"] is None


def test_a_host_without_a_collector_sends_an_absent_block(monkeypatch) -> None:
    """An older runner's beat is unchanged: the key is present and null."""
    identity = enrollment.HostIdentity(host="h1", credential="c")
    beat = enrollment.HostHeartbeat(base_url="http://cp", identity=identity)
    bodies: list[dict] = []

    def record(url, body, bearer=""):
        bodies.append(body)
        beat._stop.set()
        return {"status": "online", "heartbeat_interval_seconds": 0.001}

    monkeypatch.setattr(enrollment, "_post", record)
    beat._run()

    assert bodies[0]["capacity"] is None
