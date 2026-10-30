"""The runner's side of credential renewal: keep it first, then use it."""

from __future__ import annotations

import pytest

from cloud_agent_host import enrollment
from cloud_agent_host.enrollment import HostIdentity


@pytest.fixture()
def credential_file(tmp_path, monkeypatch):
    path = tmp_path / "credential"
    monkeypatch.setenv(enrollment.ENV_CREDENTIAL_FILE, str(path))
    monkeypatch.delenv(enrollment.ENV_HOST_CREDENTIAL, raising=False)
    enrollment.store_credential("aht.current")
    return path


def test_a_renewal_is_stored_before_the_host_switches_to_it(credential_file):
    identity = HostIdentity(host="gpu-1", credential="aht.current")
    enrollment._adopt_renewed_credential({"credential_renewal": "aht.renewed"}, identity)
    assert identity.credential == "aht.renewed"
    assert credential_file.read_text() == "aht.renewed"
    assert oct(credential_file.stat().st_mode & 0o777) == "0o600"


def test_a_renewal_that_cannot_be_stored_leaves_the_current_credential(
    credential_file, monkeypatch
):
    identity = HostIdentity(host="gpu-1", credential="aht.current")

    def refuse(_credential):
        raise OSError("read-only")

    monkeypatch.setattr(enrollment, "store_credential", refuse)
    enrollment._adopt_renewed_credential({"credential_renewal": "aht.renewed"}, identity)
    assert identity.credential == "aht.current"


def test_anything_but_a_host_credential_is_ignored(credential_file):
    identity = HostIdentity(host="gpu-1", credential="aht.current")
    for answer in ({}, {"credential_renewal": None}, {"credential_renewal": "ajt.worker"}):
        enrollment._adopt_renewed_credential(answer, identity)
    assert identity.credential == "aht.current"
    assert credential_file.read_text() == "aht.current"


def test_a_second_runner_on_the_machine_picks_up_the_stored_renewal(credential_file):
    other = HostIdentity(host="gpu-1", credential="aht.current")
    enrollment.store_credential("aht.renewed")
    enrollment.reload_credential(other)
    assert other.credential == "aht.renewed"


def test_an_injected_credential_is_never_renewed(credential_file, monkeypatch):
    monkeypatch.setenv(enrollment.ENV_HOST_CREDENTIAL, "aht.injected")
    assert not enrollment.credential_renewable()
    identity = HostIdentity(host="gpu-1", credential="aht.injected")
    enrollment._adopt_renewed_credential({"credential_renewal": "aht.renewed"}, identity)
    assert identity.credential == "aht.injected"
