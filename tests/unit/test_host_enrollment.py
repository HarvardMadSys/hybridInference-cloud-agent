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
from cloud_agent_host import enrollment


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
    sent: dict[str, Any] = {}

    def fake_urlopen(request, timeout=None):
        sent["url"] = request.full_url
        sent["body"] = json.loads(request.data)
        # `enrolling`, which is what the control plane actually answers a
        # registration with: the credential is issued, but the machine has not
        # heartbeated yet. See `AgentJobStore.register_host`.
        return _Response({"host": "h1", "credential": "hc_secret", "status": "enrolling"})

    monkeypatch.setattr(enrollment.urllib.request, "urlopen", fake_urlopen)

    identity = enrollment.ensure_enrolled(base_url="http://cp", host="h1", slots_total=4)

    assert identity is not None
    assert identity.credential == "hc_secret"
    assert sent["body"]["enroll_token"] == "enroll-once"
    assert sent["body"]["slots_total"] == 4
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
    escaped `_HostHeartbeat._run`'s `except EnrollmentError`, ending the thread.

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


def test_establish_residency_hands_back_the_generation_and_the_grant(monkeypatch) -> None:
    """The wire boundary of design §14: the fence and the thread-attributed
    credential arrive together, and a workspace-only answer (no credential)
    collapses to ``credential=None`` rather than an empty string."""
    identity = enrollment.HostIdentity(host="h1", credential="aht.h1.secret")
    answers = [{"generation": 3, "credential": "tok-1", "model": "glm-5.1"}]
    monkeypatch.setattr(enrollment, "_post", lambda *a, **kw: answers.pop(0))

    got = enrollment.establish_residency(
        base_url="http://cp", identity=identity, thread_id="t1", runtime_id="claude-code"
    )
    assert got == {"generation": 3, "credential": "tok-1", "model": "glm-5.1"}

    answers.append({"generation": 4, "credential": None, "model": None})
    got = enrollment.establish_residency(
        base_url="http://cp", identity=identity, thread_id="t1", runtime_id="claude-code"
    )
    assert got == {"generation": 4, "credential": None, "model": None}


def test_a_refused_or_malformed_establish_is_none(monkeypatch) -> None:
    """Refusal keeps nothing — and an answer without a real generation is a
    refusal, because an unfenced residency cannot be beaten for or dropped."""
    identity = enrollment.HostIdentity(host="h1", credential="aht.h1.secret")

    def refused(*_a, **_kw):
        raise enrollment.EnrollmentError("409 from establish")

    monkeypatch.setattr(enrollment, "_post", refused)
    assert (
        enrollment.establish_residency(
            base_url="http://cp", identity=identity, thread_id="t1", runtime_id="claude-code"
        )
        is None
    )

    monkeypatch.setattr(enrollment, "_post", lambda *a, **kw: {"credential": "tok"})
    assert (
        enrollment.establish_residency(
            base_url="http://cp", identity=identity, thread_id="t1", runtime_id="claude-code"
        )
        is None
    )
